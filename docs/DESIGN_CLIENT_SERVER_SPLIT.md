# Design — Client/server split (TUI ↔ server core)

Status: agreed. Phase 1 done; Phase 2 next (see §8, §9, §10).

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

- Socket in `$XDG_RUNTIME_DIR/megachat/server.sock` (fallback
  `~/.local/state/megachat/`), mode `0600`: filesystem permissions are the
  authentication. A runtime file next to it records pid and socket path.
- Framing: newline-delimited JSON-RPC 2.0. Notifications carry events
  (`event` method) after `events.subscribe(since)`.

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
- Startup: `--connect PATH` → use it; else a live socket → attach; else spawn
  `megachat serve` detached, wait for the socket, attach.
- Exit dialog: **Detach** (disconnect only) / **Quit** (`server.shutdown`). If
  the TUI attached to a server it did not spawn, Detach is the default.
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
- `contacts.for_protocol`, `contacts.register` (WhatsApp loads the LID cache
  from disk);
- `messages.snapshot` at `backend_ready` (about 0.28s per 45k cached messages
  for validation); `link.start|poll|finalize|submit_2fa` already run in
  `to_thread` but need per-call timeouts server side.

Open follow-ups: `mc-7kp` (socket adapter must confine or copy client-supplied
attachment paths), `mc-1t1` (unclosed sqlite connections in the test suite),
`mc-cvp` (isolate the `test_install_script` background alias from the real
lock file), `mc-kke` (convert handover/disaster-recovery scripts to
`megachat serve`).
