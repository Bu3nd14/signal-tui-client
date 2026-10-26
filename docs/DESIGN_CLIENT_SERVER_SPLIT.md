# Design — Client/server split (TUI ↔ server core)

Status: Phase 2 done (see §8, §9, §10, §11, §12). Phase 3 (optional) not started.

## 1. Problem

Today the TUI process **is** the server:

- `SignalTUI.__init__` (`tui/app.py`) builds the `BackendManager` and every
  protocol backend; `on_mount` starts the web server on a thread of the TUI
  process.
- The event pump lives in the UI layer: `tui/polling.py` drains
  `backend.poll_once()`, and `tui/events.py` mixes **domain** work
  (`ingest_message` → SQLite, receipts, edits, contact registration/merge)
  with **view** work (mounting bubbles, typing labels, list re-sort) and
  **web fan-out** (`web.bridge.push_event`).
- Consequence: without a running TUI nothing is persisted and the web UI gets
  no live events. `launcher/server.py` runs the TUI inside tmux just to keep
  the web UI alive; cache pruning is wired to the TUI's SIGINT handler.
- `web/api.py` route handlers contain domain logic (id canonicalization,
  group-sender resolution, send validation, edit/reaction handling and their
  `push_event`s), partly duplicated by the TUI (`_resolve_placeholder_name`,
  `_identify_contact`): the two UIs already drift.
- Even a protocol reaches into the web layer (`protocols/whatsapp.py`,
  `lid_warmup_done` push).

## 2. Goals

1. One binary, two roles:
   ```
   megachat serve            # headless server core + adapters (+ instance lock)
   megachat                  # TUI: attach to a running server, else spawn one
   ```
2. The **server API is the single source of truth**. Every transport is an
   adapter that only translates; no logic lives in adapters.
3. Transports: in-process (tests/tooling), Unix socket (TUI), HTTP (browser).
   TCP/remote is out of scope (Unix only is enough).
4. TUI exit offers **Detach** (server keeps running) or **Quit** (server shuts
   down: prune + stop).

## 3. Architecture

```
                 ┌──────────────── server core ────────────────┐
                 │ ChatCore: backends, connect, pump, DB,       │
                 │ send registry, prune, address-book refresh   │
                 │ ChatAPI: operation registry + EventBus       │  ← source of truth
                 └───────▲──────────▲──────────────▲───────────┘
                         │          │              │
                  in-process    JSON-RPC stream    HTTP adapter
                   adapter      (Unix socket)      (REST + WS + static SPA)
                         │          │              │
                 tests/tooling   TUI (always)      browser
```

### 3.1 Server core (`server/`)

- `ChatCore` owns the `BackendManager` and the backends, runs the connect
  workers, the single event pump and all domain handling of raw backend
  `ChatEvent`s. It never imports Textual or FastAPI.
- Backend progress (connecting, pairing, ready, failed, syncing) is published
  as `backend_status` / `backend_ready` events instead of TUI status calls.

### 3.2 Operations (registry)

Each operation is declared **once**: name, request model, response model
(pydantic). Examples: `contacts.list`, `messages.page`, `messages.send`,
`messages.edit`, `reactions.set`, `chats.mark_read`, `chats.archive`,
`address_book.list`, `link.start`, `media.get`, `server.status`,
`server.shutdown`.

- Neutral error model: `ApiError(code, message)` with codes such as
  `invalid_request`, `not_found`, `unauthorized`, `too_large`, `backend_down`.
  Each adapter maps codes to its own form (HTTP status, JSON-RPC error,
  exception).
- The HTTP route table and the JSON-RPC method table are generated from (or
  validated against) the registry; a JSON Schema is exported from it.

### 3.3 Events (`EventBus`)

- Typed envelope `{seq, type, payload}` with **full** payloads (message,
  message_edit, reaction, receipt, typing, contact_update, contact_merged,
  backend_status, backend_ready, lid_warmup_done, …).
- Multi-subscriber, thread-safe, monotonic `seq`, bounded ring buffer of
  recent events: `subscribe(since=seq)` replays what a reconnecting client
  missed, or signals a `gap` (client must resync) when `since` fell out of
  the buffer or the subscriber's queue overflowed. Producers never block.

### 3.4 Blobs

Media, avatars, thumbnails, uploads are a separate concept: an operation
returns a blob handle and each adapter delivers it its own way — HTTP streams
it (cache headers, `?v=`, `?w=`, Range), the Unix socket returns a local file
path (same machine), in-process returns the path. Uploads mirror this
(multipart parsed by the HTTP adapter into file handles).

## 4. Adapters

### 4.1 In-process

Direct calls into `ChatAPI`; used by tests and tooling. In tests the request
and response go through the models so non-serializable values are caught.

### 4.2 Unix socket (TUI)

Implemented by `server/socket_adapter.py` (imports only `server.*`:
`api`, `bus`, `models_api`, `uploads`, `version`), started by `megachat serve`
unless `--no-socket` (`--socket PATH` overrides the location; `--no-web`
leaves the socket as the only adapter). `ChatCore.shutdown` stops the socket
adapter (before the web adapter), which removes the socket and runtime file. Everything below is the wire
contract the `SocketClient` (P2.2) implements.

#### Location, permissions, runtime file

- Socket: `$XDG_RUNTIME_DIR/megachat/server.sock`; without
  `XDG_RUNTIME_DIR`, `~/.local/state/megachat/server.sock`. Overrides:
  `megachat serve --socket PATH`, else the env var `MEGACHAT_SOCKET`
  (tests use both). `sun_path` is limited to ~100 bytes: a longer path is an
  error at startup, not a silent truncation.
- A directory the adapter creates is `0700`; an existing default
  `megachat/` directory owned by us is tightened to `0700`; the parent of an
  explicit `--socket` path that already exists is left alone. The socket is
  bound under `umask 077` and then `chmod 0600` (no window in which another
  uid can connect). Filesystem permissions are the authentication.
- On Linux the adapter also checks `SO_PEERCRED` on every accepted
  connection and drops peers whose uid differs from the server's effective
  uid (defence in depth, e.g. a wrongly shared directory); platforms without
  `SO_PEERCRED` rely on the permissions alone.
- Runtime file `server.json` in the socket's directory (`0600`):
  `{"pid", "socket", "version", "api_version", "started_at"}`
  (`version` = server version string, `api_version` = int protocol version,
  `started_at` = Unix time, float). It is written atomically (temp file +
  `rename`) only after the socket listens, and removed on shutdown (the
  socket and the file are only removed if they are still the ones this
  server created).
- Startup finds an existing path: not a socket, or owned by another uid ->
  refuse (never unlink what we did not create); a socket nobody accepts on
  (connect refused: the previous server crashed) -> unlink it and the old
  runtime file, continue; a socket that accepts -> a live server answers:
  refuse to start (`megachat serve` exits 1). The single-instance lock
  (`server/lock.py`) already prevents two servers; this protects the path
  when the lock is bypassed.

#### Framing

Newline-delimited JSON-RPC 2.0: one JSON object per line, UTF-8, `\n`
terminated, no batches (an array is answered with `-32600`). The server
writes with `ensure_ascii` (no raw U+2028 etc. in lines). A line longer than
`MAX_LINE_BYTES` (16 MiB) is answered with a `too_large` error (id `null`)
and the connection is closed (the stream cannot be resynchronised). A line
that is not valid UTF-8/JSON is answered with `-32700` (id `null`) and the
connection stays open. Blank lines are ignored. A connection that does not
drain its writes for 30 s is dropped. At most 32 concurrent connections (the
33rd gets `-32031` with id `null`, then is closed). A client may half-close
after sending: requests already received are still answered, then the server
closes. When the server closes after a protocol error with unread input
pending, the peer may see `ECONNRESET` after the error line: read what was
delivered first.

#### Requests and responses

Request: `{"jsonrpc":"2.0","id":1,"method":"contacts.list","params":{...},
"timeout": 30}`. `id` is a string or an integer (any other type is `-32600`);
a request without `id` is a notification: executed, never answered. `params`
is the operation's request object (absent/`null` = `{}`; a non-object is
`-32602`). The non-standard envelope member `timeout` (seconds, `0 <
timeout <= 600`) sets this call's server-side time limit; the default is 120
s (`DEFAULT_TIMEOUT_S`). Success: `{"jsonrpc":"2.0","id":1,"result":<call_json
output>}`.

Requests of one connection run concurrently and may be answered in any
order (match by `id`; at most 64 in flight per connection, beyond that
`-32030`). Operations never block the loop: sync operations run in a bounded
thread pool, async ones (`messages.send`) on the adapter loop (their internal
`to_thread` uses a second bounded pool), so a slow call blocks neither other
connections nor this connection's event notifications. A timeout answers
`-32020` (`data.code` `"timeout"`) but **does not cancel** the operation
(threads cannot be interrupted and a half-done send must not be torn down):
its result is discarded, so a client must treat a timed-out mutation as
unknown and re-sync.

Errors: `{"error":{"code":N,"message":"...","data":{"code":"<ApiError code>",
"message":"...","details":...}}}` where `data.message` is the exact
user-facing `ApiError.message`.

| ApiError code | JSON-RPC code | | ApiError code | JSON-RPC code |
|---|---|---|---|---|
| `invalid_request` | -32602 | | `unprocessable` | -32006 |
| `not_found` | -32001 | | `not_supported` | -32007 |
| `unauthorized` | -32002 | | `backend_down` | -32008 |
| `forbidden` | -32003 | | `backend_error` | -32009 |
| `too_large` | -32004 | | `internal` | -32603 |
| `conflict` | -32005 | | | |

Protocol-level errors: `-32700` parse error, `-32600` invalid request
(envelope), `-32601` unknown method (`data.code` `"method_not_found"`),
`-32602` invalid params, `-32603` internal error (unexpected exception,
details only in the server log), `-32020` timeout, `-32030` too many requests
in flight, `-32031` too many connections. Everything else (`-320xx`) maps from `ApiError` as above.

#### Methods

- Every registered operation, by its registry name.
- `rpc.hello` -> `{"api_version", "server_version", "operations": [names]}`;
  clients call it first and refuse an `api_version` they do not know.
- `server.ping` -> `{"pong": true, "ts": <float>}`.
- `events.subscribe {"since"?: N}` -> `{"subscribed": true, "last_seq": M}`.
  Afterwards the server sends notifications
  `{"jsonrpc":"2.0","method":"event","params":{"seq","type","payload","ts"}}`
  for every bus event (including `gap`), exactly the `/ws/v2` frame: events
  with `seq > N` still in the ring buffer are replayed first, a `gap` event
  (`reason` `buffer` or `overflow`) tells the client to resync, and while
  idle a `{"method":"heartbeat","params":{"ts"}}` notification arrives every
  5 s. `since` must be an int >= 0 (else `-32602`); without `since` the stream
  is live only. One subscription per connection: a second `events.subscribe`
  restarts the stream. Events are delivered in order; a client that reads too
  slowly overflows into a `gap` rather than blocking the bus.
- `events.unsubscribe` -> `{"subscribed": false}` (idempotent).
- `server.clients` -> `{"connections": N, "self_included": true, "web": W}`:
  live socket connections (the caller's own included, hence `self_included`)
  and, separately, browser WebSockets of the web adapter. Adapters register a
  counter in `ChatAPI.client_counters`; `connections` is 0 and
  `self_included` false when no socket adapter runs.
- `server.shutdown` -> `{"status": "ok"}`: answers at once, then the host's
  `ChatAPI.shutdown_hook` runs ~0.2 s later on its own thread (`megachat serve`
  installs `stop.set`, the same event SIGINT sets: ordered shutdown, prune,
  lock/socket/runtime file removed, exit 0). `not_supported` when there is no
  hook (in-process core). The socket adapter drains the connections' output
  before aborting them, so the answer cannot be lost to the stop.

#### Attachment paths (mc-7kp)

`messages.send` trusts `AttachmentIn.path`; HTTP only passes files it stored
itself and keeps that behaviour. A socket client is the same uid on the same
machine, so reading its files is no privilege escalation, but the operation
must not be steered into attaching something the user did not choose.
Decision: **copy, do not trust.** Before calling the operation the adapter
stages every attachment: the path must be absolute; it is opened with
`O_NOFOLLOW|O_NONBLOCK` (a symlink as the last component is refused), the
open descriptor must be a regular file (no FIFO, device, directory, socket),
and its bytes are copied into the upload directory (`0600`, size/kind guard
of `server/uploads.py` applied while copying, 250 MiB total, at most
`MAX_ATTACHMENTS` files). The operation then validates the staged copy
(sniffed kind, per-kind limits, extension) and only sees paths the server
owns; staged files are deleted when the call ends. A file that is not an
acceptable attachment (a key file, a text file) is therefore rejected by the
same content policy as an upload, never sent. The payload is pre-checked with
`messages.send.check` before any copy so invalid requests cost no I/O.
Because the filename decides the extension check, a missing `filename`
defaults to the basename of the path. Confining paths to home/tmp was
rejected: it blocks legitimate mounts and still trusts content.

`gmail.messages.send` (mc-im6) has the same policy with two differences,
because a mail attachment is any file, not media: the copy skips the per-kind
media limits (`stage_local_file(media_limits=False)`) and is capped at the Gmail
per-file limit (25 MiB); the operation then enforces the Gmail limits (10
files, 25 MiB each and in total) on the copies. Its pre-check is
`gmail.messages.send.check`. The adapter keeps one table of staging operations
and their checks (`STAGING_CHECKS`), and still refuses to start for an
operation with `attachments` that has no policy.

#### Client: `server/socket_client.py`

`SocketClient.connect(path=None, timeout=5)` implements `ChatClient` over the
contract above (`call(name, data, timeout=)`, `acall`, `subscribe(since)`,
`close`; `connected`/`state`; path resolution from `server/paths.py`, shared
with the adapter). One socket, one supervisor thread that reads it and
reconnects; `call` is thread safe (ids, out-of-order answers). Errors are all
`ApiError`s: server errors keep their code; client-side codes
`server_unavailable` (`ServerUnavailable`: connection down/refused/silent,
client closed; a call in flight then has an unknown outcome),
`timeout` (`OperationTimeout`: the operation is not cancelled, outcome
unknown) and `incompatible_server` (`IncompatibleServer`: `rpc.hello`
`api_version` differs; final, also on reconnect).

Liveness: no bytes for `heartbeat_timeout` (15 s = 3 heartbeats) kills the
connection; a client without subscription pings every 5 s instead.
Reconnect: bounded jittered backoff (0.1 s to 5 s, forever unless
`reconnect_timeout`); calls fail immediately meanwhile. The event stream is
resumed with `events.subscribe {since: last seq seen}`. Subscribers get the
synthetic event `client_connection {state: "lost"|"restored", reason}`
(seq = last server seq, never advances the stream; consumers ignore unknown
types) and, when events cannot be replayed (`gap` from the server) or the
server restarted (runtime file pid/`started_at` changed, or `last_seq` went
backwards), a `gap` event (`reason: "restart"` for a restart) so the frontend
resyncs. All subscriptions of a client share one server-side stream; later
ones are served from a ring of the last 2000 events.

### 4.3 HTTP (browser)

- **Option A (now):** keep the current REST shapes used by `web/static`;
  every JSON route shrinks to “parse → call operation → map result/errors”.
  The quirks (`proto` vs `protocol`) live only in the translation.
- **Option B (Phase 3, optional):** generic `POST /api/rpc/{op}` generated
  from the registry and the SPA migrated to it.
- Stays REST forever: blob routes (`/api/media/...`, `/api/contact-avatar`,
  `/api/link-preview/image`, `/api/quote-media/...`) used as `<img>/<video>`
  URLs; multipart `/api/send`; unauthenticated `/health`.
- WebSocket: `/ws` keeps the legacy thin notification shape through a pure
  field mapping from core events (`message_cache.js` keeps refetching);
  `/ws/v2` exposes full core events with resume.

### 4.4 Drift guards

1. Import rule test: `web/` (and the socket adapter) may import only
   `server.api` / its models, never `protocols.*` or `tui.*`.
2. Contract tests: one suite run against every adapter; REST outputs compared
   with the core operation output after mapping.

## 5. TUI client

- The TUI only knows `ChatClient` (socket transport in production,
  in-process transport in tests).
- Startup (`signal_tui.main`, logic in `server/attach.py`):
  1. `--connect PATH` → `SocketClient.connect(PATH)`; if nothing answers, exit 1
     with a message. Never spawns.
  2. else a live server on the default socket path (`server/paths.py`:
     `rpc.hello` answers) → attach. A server with another wire version is an
     error (exit 1), not a reason to spawn.
  3. else spawn `megachat serve --socket <path>` and attach as soon as its socket
     answers (bounded: 30 s). The child is `sys.executable megachat.py serve`
     in the same checkout, `start_new_session=True` (it survives the TUI and
     its terminal; the TUI's Ctrl+C does not reach it), stdin `/dev/null`,
     stdout/stderr appended to `/tmp/megachat-serve.log`, and a daemon thread
     reaps it. Web: `--no-web`, unless the user passed `--web`/`--web-port`/
     `--web-host`/`--web-no-auth` or `config.json` enables the web UI; then the
     flags are passed through and the server serves the web UI (TUI-side web
     flags are ignored, with a note, when attaching to an existing server).
     `--debug` is passed through too. If the child exits before answering and
     nobody holds the instance lock, exit 1 with its exit code and the tail of
     its output; on timeout or Ctrl+C the child we spawned is sent SIGTERM.
  4. `--in-process` (debugging/tests, mutually exclusive with `--connect`):
     the TUI owns `ChatCore` + `InProcessClient`, the embedded web adapter and
     the lock, as before Phase 2.
  The TUI records whether it spawned the server (`SignalTUI.server_spawned`:
  `True` spawned, `False` attached, `None` in-process); the exit dialog's
  default depends on it (below). See the exit table below for what quitting does.
- Lock: `/tmp/signal-tui.lock` (`$MEGACHAT_LOCK_FILE` overrides it) belongs to
  whoever owns the backends: the server, or a `--in-process` TUI. An attached
  TUI never takes it, so many TUIs share one server. Check-and-write is
  serialised by an `flock` on `<lock>.guard`. Two TUIs started together may
  each spawn a server; exactly one wins the lock, the loser exits ("already
  running") and its TUI notices (the child exited while a live process holds
  the lock), keeps waiting and attaches to the winner (`spawned=False`).
  `launcher.py server stop`/`web-signal-tui-stop` still SIGINT the lock pid,
  which now stops the server (attached TUIs reconnect-loop).
- Logs: `megachat serve` and `--in-process` log to `/tmp/signal-tui.log`; the
  TUI client logs to its own `/tmp/megachat-tui.log`; the output of a spawned
  server (startup errors) is in `/tmp/megachat-serve.log`.
- Exit (`tui/exit_dialog.py`, flow in `tui/exit_flow.py`):

  | Situation | Result |
  |-----------|--------|
  | `--in-process` (the TUI owns the core) | no dialog; Ctrl+Q quits as before |
  | Ctrl+Q, attached or spawned, server reachable | dialog **Detach** / **Quit** / **Annulla** |
  | Ctrl+Q, server lost or `server.clients` says `ServerUnavailable` | no dialog, exit |
  | Ctrl+C / SIGINT, attached or spawned | no dialog, **Detach**; never stops the server |
  | Detach | close the client, exit; the server keeps running |
  | Quit | `server.shutdown`, wait until the server process is gone (<= 30 s), close the client, exit |
  | Quit, `server.shutdown` -> `ServerUnavailable` | exit anyway |
  | Quit, any other error (`not_supported`, timeout, ...) | error in the status bar, stay (as Cancel) |

  The default button is Quit when `server_spawned` is True (this TUI started
  the server) and Detach otherwise. Before showing the dialog the TUI asks
  `server.clients` (worker); other clients (`connections` minus itself, plus
  web sockets) are announced ("Altri N client connessi") and Quit then needs a
  second confirmation (Cancel focused, Detach no longer offered). Keys: `d`,
  `q`, `Esc` = Cancel, arrows/`Tab`, `Enter`. Quit reads the server pid from
  the runtime file before asking (the file disappears early in the stop) and
  waits for that process to exit, so no orphan server is left behind.
- Optimistic send: client-generated `client_msg_id`, confirmed by the
  server's `message` event (the existing `server/send_registry.py` mechanism
  moves to the core).
- View-only state stays in the TUI (selection, filters, typing timers,
  progressive render, widget maps); domain state comes from operations and
  events.

## 6. Phases

Each phase ships on its own with the full test suite green.

- **Phase 0 — core extraction + `serve`.**
  1. `server/bus.py`: `EventBus` (seq, ring buffer, resume, gap).
  2. `ChatCore` owns manager/backends and connect workers; status reported via
     events; TUI consumes them (still in-process, `self.manager` kept as a
     compatibility alias).
  3. Event pump + domain handling move from `tui/events.py`/`tui/polling.py`
     into the core; the TUI keeps view handling only and subscribes to the
     bus. `push_event` disappears from `tui/` and `protocols/`; the web
     WebSocket subscribes to the bus through the legacy mapping.
  4. `megachat serve` entry point (headless: lock, logging, core, web
     adapter, SIGINT → prune + stop); `launcher/server.py` uses it instead of
     running the TUI in tmux.
  5. Operation registry skeleton + move the domain logic out of the
     `web/api.py` route handlers into operations (route by route).
- **Phase 1 —** in-process adapter/client, TUI on `ChatClient`, HTTP adapter
  fully on operations (Option A), contract tests, import rule.
- **Phase 2 —** Unix-socket JSON-RPC adapter + client, events with resume,
  blob handles, discovery, attach-or-spawn, Detach/Quit dialog.
- **Phase 3 —** optional: SPA moved to `/api/rpc/{op}` and `/ws/v2`.

## 7. Risks

- Event ordering: the TUI previously did ingest and view in the same thread
  step; after the split view handlers run on core events that already carry
  the ingest result (`added` / `changed`, contact snapshot).
- In Phase 0 event payloads may still carry in-process objects
  (`ChatContact`); Phase 1 makes every payload serializable.
- Large test surface (≈150 test files) relies on calling TUI handlers
  directly; Phase 0 keeps compatibility wrappers so tests change only where
  behaviour moves.

## 8. Phase 0 outcome

What exists now:

- `server/bus.py` — `EventBus` (seq, replay buffer, `subscribe(since)`, GAP).
- `server/core.py` — `ChatCore`: backends, connect workers, the single event
  pump, domain handling (`process()`), address-book refresh; `core.api` is the
  `ChatAPI`.
- `server/api.py` + `server/models_api.py` + `server/ops/` — operation
  registry. Operations: `emoji.list`, `reactions.available`, `server.status`,
  `contacts.list`, `contacts.book`, `chats.mark_read`, `chats.archive`,
  `messages.page`, `messages.edit`, `reactions.set`, `messages.send`,
  `messages.send.check`.
- `server/main.py`, `megachat.py`, `python -m server` — headless
  `megachat serve`; `launcher server start` uses it instead of tmux.
- `web/` JSON routes above are translations onto operations (`web/ops.py`
  `call_op`); `/ws` frames come from the bus via `web/legacy_events.py`.
  Contract tests compare every migrated route with its operation.

Decisions taken during Phase 0:

- `messages.page` returns `list[dict]`, not a strict model: rows carry
  optional keys and the SPA relies on exact JSON equality.
- Uploads: policy (kinds, per-kind limits, count) is domain
  (`server/uploads.py`); temp storage and the request-wide byte cap are
  transport (`web/uploads.py`).
- `messages.send.check` is a side-effect-free pre-check the HTTP adapter runs
  before storing uploads, so rejected requests do no upload I/O. The
  `client_msg_id` claim applies to text-only sends, as before.
- New error codes beyond §3.2: `backend_error` (502), `unprocessable` (422),
  `not_supported` (501).
- Operations still publish the legacy `/ws` frames as `web_push` events
  (`ChatAPI.publish_web_push`); typed events replace them in Phase 1.

Still REST-only (Phase 1/2, blob handles): media, quote-media, avatar,
link-preview, transcribe, config, log routes. The TUI still calls backends
directly for its own send/edit/load paths (Phase 1, `ChatClient`).

Notes for Phase 2: the socket adapter must confine or copy client-supplied
attachment paths (`messages.send` trusts `AttachmentIn.path`).

## 9. Phase 1 / P1.7 outcome (adapters see only the API)

- `start_web_server(api, port, token, *, host, require_auth, bus)`: the web
  adapter gets only the `ChatAPI`; `web/` imports `server.api`,
  `server.models_api`, `server.bus` and `server.uploads` and nothing else of
  `server`, `protocols`, `tui` or `transcription` (enforced in
  `tests/test_server_import_rules.py`). `tui/` and the root UI modules import
  neither `protocols` nor `web`; the TUI gets its embedded web adapter injected
  by the composition root `signal_tui.py` (which may import `protocols.config`
  and `web`, as it runs before the core exists).
- Operations added: `links.preview`, `links.preview_image` (blob),
  `server.client_log`, `transcribe.submit|status|config`,
  `config.set_openai_key`; `server.config` also carries the picker options.
  The transcription service is core state (`ChatAPI.transcription`, started by
  `ChatCore.start`, stopped by `ChatCore.shutdown`).
- `/ws/v2` (same authentication as `/ws`): the client may send
  `{"type":"subscribe","since":N}` (or use `?since=N`); the server streams
  `{"seq","type","payload","ts"}` for every bus event, replays from the ring
  buffer, sends a `gap` event (`reason` `buffer` or `overflow`) when the client
  must resync, and `{"type":"heartbeat"}` when idle. `/ws` is unchanged.

## 10. Phase 1 outcome

What exists now:

- `server/client.py` — `ChatClient` (`call`, `acall`, `subscribe(since)`,
  `close`) and `InProcessClient(core)`, the only transport so far. The TUI
  talks to the core through `self.client` alone: it reads no backend, no
  manager and no SQLite (`tests/test_tui_no_backend_access.py`).
- `server/events_api.py` — one pydantic model per bus event (`EVENT_MODELS`),
  `ChatCore.process()` returns JSON-serializable wire payloads
  (`ContactWire` instead of `ChatContact`); a test guard asserts
  `json.dumps` on every published event.
- Operations added in Phase 1 (on top of §8): `media.*` and `contacts.avatar`
  (blobs: `BlobOut` = local path + type/size/version), `contacts.for_protocol|
  register|address_book|touch|check_number`, `messages.window|snapshot`,
  optimistic `messages.send` (`client_msg_id`, no `sent_mirror`, single
  attempt) and view-only edit/reaction on top of `messages.edit`/`reactions.set`,
  `download.serve_*`, `link.*` (device link service owned by `ChatAPI.link`),
  `server.config`, `links.preview|preview_image`, `server.client_log`,
  `transcribe.*`, `config.set_openai_key`.
- `/ws/v2` streams the typed bus events with `since` resume and `gap`; `/ws`
  keeps the legacy frames.
- Import rules are enforced by tests (§9): `web/` sees only the API, `tui/`
  imports neither `protocols` nor `web`, `server/` imports neither `tui` nor
  `web` (except lazily in `main.py`).
- No test-only plumbing in production: `SignalTUI._handle_event`, the
  `_compat_payload_ids` skip list in the poll worker and `web/bridge.py` are
  gone. Tests feed raw backend events with `tests/event_harness.feed(app, raw)`:
  `core.process(raw)` -> publish on `app.bus` -> the app's own consumer step
  (`PollingMixin._route_bus_event`, the one the poll worker calls). View errors
  propagate there (the worker's `_dispatch_bus_event` wrapper logs them).

Decisions:

- `web/bridge.py` (a process-global bus) was removed; the web app and
  `/ws` take the bus from `app.state.bus` (`start_web_server(..., bus)`, else
  `api.bus`), and route handlers publish through `ChatAPI.publish_web_push`.
- `publish_web_push` / the `web_push` event keep their names: the legacy
  `/ws` frames depend on them and renaming gains nothing. Typed events for the
  same changes already exist on `/ws/v2`; `web_push` goes away with `/ws`
  (Phase 3).
- The TUI cache stays row-for-row what the backend cache held
  (`messages.window`/`snapshot` return core cache rows, not `messages.page`
  rows), so optimistic rows and merge rules remain view-side.
- Contact objects in the TUI are copies of wire contacts; the core keeps
  backend-owned recency in sync (`bump_recency`, `sync_recency`).

Sync operations still called on the TUI UI thread (Phase 2 must make them
non-blocking, cached or prefetched, as each becomes a socket round trip):

- `link.status`, `link.cancel`, `link.reconnect`;
- `download.serve_attachment`, `download.serve_text`
  (`on_image_widget_image_clicked`, `_start_download`);
- `server.config` (thumbnail sizes in `chat_view`, picker settings at
  `on_mount`);
- `contacts.register` (WhatsApp loads the LID cache from disk).

Already off the UI thread: `messages.snapshot` and `contacts.for_protocol` at
`backend_ready` are prefetched by the poll worker
(`_prefetch_backend_ready`); the UI-thread merge only fetches them itself
when called directly. `link.start|poll|finalize|submit_2fa` run in
`to_thread` but need per-call timeouts server side.

Open follow-ups: `mc-7kp` (socket adapter must confine or copy client-supplied
attachment paths), `mc-1t1` (unclosed sqlite connections in the test suite),
`mc-cvp` (isolate the `test_install_script` background alias from the real
lock file), `mc-kke` (convert handover/disaster-recovery scripts to
`megachat serve`).

## 11. Phase 2 / P2.3 outcome (the TUI over a remote client)

The TUI works with any `ChatClient`; with a `SocketClient` a call is a round
trip that can be slow or fail. Rules, implemented in `tui/client_calls.py`
(`ClientCallsMixin`) and `tui/client_timeouts.py`:

- **No client call on the UI thread.** Call sites run in a worker
  (`_offload`) and apply the result with `_ui` (inline on the UI thread,
  `call_from_thread` elsewhere). Without a running app (plain unit tests)
  `_offload` runs inline. The former UI-thread calls moved as follows:
  `link.status` (Ctrl+L opens the modal when the answer arrives),
  `link.cancel` (fire and forget thread, ordered before `link.reconnect`),
  `link.reconnect`, `download.serve_*` (the link widget is mounted, and
  download mode left, when the server answers), `media.attachment` of an
  image click, `contacts.register` (opening a contact not yet listed shows
  "Apertura chat…" and opens the chat when the server answers), the
  `backend_ready` fallback fetches, and `server.config` (fetched once by a
  worker at mount and on resync; thumbnail limits use the model defaults until
  it arrives).
- **Timeouts** per operation, one table (`OP_TIMEOUTS`); an operation missing
  from it raises `KeyError`, and a test checks every operation name the TUI
  uses is listed.
- **Errors.** `ServerUnavailable` and `OperationTimeout` show a transient
  status message (at most one per 5 s, none while the persistent "lost"
  message is up); `IncompatibleServer` a persistent one. A mutation that
  timed out or lost its connection has an unknown outcome: a send stays
  `pending` (never `failed`, a retry could duplicate it), an edit keeps its new
  text, and the resync settles both. Other `ApiError`s are handled as before.
- **Resync** (worker, coalesced, UI-thread merge) on `gap`, on
  `client_connection` `restored`, after a timed-out mutation (at most every
  10 s) and when the TUI attaches to a server it does not own: `server.status`,
  then for each connected protocol `messages.snapshot` + `contacts.for_protocol`
  (merged by `cache_key`, add-only), the open chat's `messages.window`, unread
  badges. A changed open chat is remounted from the merged cache. Pending
  bubbles the server has no trace of become `failed`; ones it knows take its
  status.
- `SignalTUI(client=...)` injects a client; the TUI then does not own the core
  (it neither starts nor shuts it down). Attach-or-spawn is P2.4.

## 12. Phase 2 outcome

What exists now:

- `megachat serve` listens on a Unix socket (`server/socket_adapter.py`,
  JSON-RPC over newline-delimited JSON, `events.subscribe` with resume,
  `0600` socket in a `0700` dir, `SO_PEERCRED` check, runtime file next to the
  socket). `server/socket_client.py` is the `ChatClient` over it, with
  reconnect and `since` resume.
- The TUI is always a client (`server/attach.py`): `megachat` attaches to the
  server on the socket or spawns `megachat serve` detached and attaches;
  `--connect PATH` never spawns; `--in-process` keeps the old
  core-inside-the-TUI mode (debugging/tests) and refuses to run next to a
  server (it needs the instance lock, which belongs to `serve`).
- Exit: Ctrl+C / SIGINT = Detach (the server keeps running); Ctrl+Q opens the
  Detach/Quit dialog; Quit calls `server.shutdown` and waits for the server to
  go away. `server.shutdown` and `server.clients` are ordinary operations.
- `scripts/smoke_e2e.sh` (`make smoke`, CI job `smoke`) drives all of this with
  real processes in an isolated HOME/lock/socket.

Decisions: the socket is the only authentication boundary (filesystem
permissions); the server owns the lock, attached TUIs never take it; the
default dialog button is Quit only when this TUI spawned the server;
SIGINT never stops a server; the TUI makes no client call on the UI thread.

Known limitations:

- Any client of the same uid can call `server.shutdown` (and every other
  operation): there is no per-client authorization.
- Quit waits up to 30 s for the server to go away, then exits anyway (the
  server may still be stopping); "other clients" in the dialog is a snapshot.
- A mutation (send, edit) that timed out or lost its connection has an
  unknown outcome: the TUI keeps it `pending` and lets the resync settle it;
  it is never retried automatically.
- Resync is add-only and merged by `cache_key` (no deletions are propagated);
  it covers a `gap`, a reconnect and a timed-out mutation, and re-reads only
  the open chat's window, not the history of every chat.
- Log files are fixed paths in `/tmp` (`signal-tui.log`, `megachat-serve.log`,
  `megachat-tui.log`), also in the smoke run.
- The smoke needs `script` (util-linux) and Linux (`/proc`); it uses no real
  backend, so backend behaviour is covered by the unit/integration suites.

## 13. Gmail moved into the core (mc-615, mc-im6)

`origin/master` gained a Gmail reader (IMAP/SMTP, HTML sanitizer, signed image
proxy, the Gmail settings in `protocols/config.py`) that lived inside the web
adapter. The master merge (mc-615) kept it web-only behind a narrow, temporary
import exemption; bead mc-im6 removed both. **Gmail is now core logic behind
operations and `web/` has no Gmail-specific exception to the import rules.**

| piece | where |
|---|---|
| IMAP/SMTP client, MIME parsing, sanitizer, caches, IMAP pool, image-token signing, temp blobs | `server/gmail.py` (may import `protocols.config`, like the rest of `server/`) |
| operations | `server/ops/gmail.py`, models in `server/models_api.py` |
| HTTP translation | `web/gmail_api.py` (`/api/gmail/*`, `/gmail-image`) |

Operations: `gmail.status`, `gmail.messages.list`, `.get`, `.delete`,
`.bulk_delete`, `.mark_read`, `.send`, `.send.check`, `gmail.attachment`,
`gmail.image`. Response shapes equal the JSON the routes always sent
(`dict` results); errors keep their HTTP status and Italian texts: not
configured `backend_down` (503), validation `invalid_request`/`too_large`
(400/413, the Gmail text), missing `not_found` (404 "Risorsa non trovata"),
upstream/transport `backend_error` (502 "Errore di comunicazione con Gmail"),
anything else `backend_error` (502 "Errore Gmail"). The blocking IMAP/SMTP calls
run in the operation's worker thread (`call_op` / the socket's sync pool); only
`gmail.image` is `async` (it uses the shared HTTP session).

Blobs. `gmail.attachment` (`GmailAttachmentOut`: a `BlobOut` plus `filename`)
and `gmail.image` (`BlobOut`) return a **temporary** file: the bytes come from
the network, so they are written `0600` to `<cache dir>/gmail-blobs` (`0700`).
The adapter that delivers one deletes it (the HTTP routes do, right after
reading it); the core prunes files older than 15 minutes at every write and
wipes the directory at shutdown. The image proxy keeps its protections
unchanged: `gmail.image` only fetches URLs this server HMAC-signed
(`sign_image_url`/`verify_image_token`, key derived from the web token), through
the SSRF-safe public-address fetcher of `server/link_preview.py` (redirects
re-validated, raster magic bytes only, 8 MiB cap).

Sending. `/api/gmail/send` stays multipart: the route parses the form, copies
the files to temp files with `web/uploads.py` (per-file cap = the Gmail limit,
no media policy) and passes them as `AttachmentIn` handles; the operation
validates count, slots and sizes (in the old order: first oversized file, then
the total), reads the bytes and sends. `gmail.messages.send.check` runs
first, so too many attachments or a non-file slot cost no disk I/O. Over the
socket the same operation has an explicit staging policy (section 4.2).

Lifecycle. Two resources moved to the core, with different owners because of
what they are bound to:

- The **IMAP pool** (threads, sockets; no loop) is closed by
  `ChatCore.shutdown` through `ChatAPI.close_resources()`, after the adapters
  have stopped (so no request is mid-flight) and before the transcription
  service stops. It is idempotent.
- The **shared HTTP session** (aiohttp, used by link previews and the Gmail
  image proxy) is bound to the event loop that created it and can only be closed
  there. Each adapter that runs operations on its own loop (web, socket) awaits
  `ChatAPI.close_loop_resources()` on that loop right before it closes; a
  session owned by another loop is left alone (so the first adapter to stop no
  longer orphans the other's). `web/server.py` therefore no longer imports
  `server.link_preview` or Gmail: it only calls the API.

`tests/test_server_import_rules.py` has no Gmail exemption any more:
`GMAIL_IMPORT_EXEMPTIONS` and `test_gmail_exemption_is_exact` are gone, and
`web/` imports only the API-layer allowlist. Link previews live in
`server/link_preview.py`; `web/link_preview.py` no longer exists.
