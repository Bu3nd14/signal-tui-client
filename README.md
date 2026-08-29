# Signal / WhatsApp / Telegram TUI Client

[![codecov](https://codecov.io/gh/Bu3nd14/signal-tui-client/graph/badge.svg)](https://codecov.io/gh/Bu3nd14/signal-tui-client)

A terminal-based (TUI) multi-protocol messaging client built with [Textual](https://textual.textualize.io/).

- **Signal**: uses `signal-cli` daemon via JSON-RPC over HTTP for fast operations, with automatic
  fallback to subprocess if the daemon is unavailable.
- **WhatsApp**: optional backend via the lightweight [WAHA](https://waha.devlike.pro/) Docker
  container — incoming messages arrive in real time through webhooks, no polling needed.
- **Telegram**: optional backend using [Telethon](https://docs.telethon.dev/) (MTProto) —
  native Python, no external daemon required. QR login with 2FA support.

![Main interface](assets/screenshots/screenshot.png)
*Main chat interface*

![Image modal viewer](assets/screenshots/screenshot2.png)
*Fullscreen image viewer modal*

![Image modal viewer (alternate)](assets/screenshots/screenshot3.png)
*Fullscreen image viewer modal (alternate view)*

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Option A — Automatic installation](#option-a--automatic-installation-recommended)
  - [Option B — Manual installation](#option-b--manual-installation)
  - [Enabling the WhatsApp backend (optional)](#enabling-the-whatsapp-backend-optional)
  - [Enabling the Telegram backend (optional)](#enabling-the-telegram-backend-optional)
- [Virtual environment](#virtual-environment-optional-but-recommended)
- [Updating signal-cli](#updating-signal-cli)
- [Device linking](#device-linking)
- [Usage](#usage)
  - [Controls](#controls)
  - [Composing messages](#composing-messages)
  - [Emoji picker](#emoji-picker)
  - [Contact search and address book](#contact-search-and-address-book)
  - [Message editing](#message-editing)
  - [Replies and quoted media](#replies-and-quoted-media)
  - [Message delivery status](#message-delivery-status)
  - [Download mode](#download-mode)
  - [Contact grouping and unread filters](#contact-grouping-and-unread-filters)
- [Native inline images (kitty graphics protocol)](#native-inline-images-kitty-graphics-protocol)
- [Performance profiling](#performance-profiling)
- [Testing](#testing)
- [Project structure](#project-structure)
- [License](#license)

## Features

| Feature | Description | Details |
|---|---|---|
| Unified multi-protocol inbox | Signal, WhatsApp, and Telegram contacts and chats in a single list, with real-time send/receive on all three protocols | [Usage](#usage) |
| **Message editing** *(new)* | Edit your own text messages after sending; incoming edits update the bubble in place | [Message editing](#message-editing) |
| **Replies with quoted media** *(new)* | Quote an image in a reply (`Alt+R` or Alt+click); incoming media quotes are displayed with caption or typed label | [Replies and quoted media](#replies-and-quoted-media) |
| **Multi-line message input** *(new)* | `Enter` sends, `Shift+Enter` / `Ctrl+Enter` / `Ctrl+J` insert a newline | [Composing messages](#composing-messages) |
| **Full address book search** *(new)* | `Ctrl+S` searches the complete address book of all three backends — including contacts with no existing chat — and can open-or-create the conversation | [Contact search and address book](#contact-search-and-address-book) |
| **Failed-send tracking** *(new)* | Messages that fail to send are marked clearly instead of being silently lost; delivery status transitions are robust to daemon echo races | [Message delivery status](#message-delivery-status) |
| **Instant chat reordering** *(new)* | After an optimistic send the chat list re-sorts by recency immediately, without waiting for the server round trip | [Message delivery status](#message-delivery-status) |
| Native inline images | High-resolution inline thumbnails and fullscreen viewer via the kitty graphics protocol; automatic `catimg` fallback everywhere else | [Native inline images](#native-inline-images-kitty-graphics-protocol) |
| Multiple attachments | A message with several photos shows each one separately (Signal and WhatsApp); clickable placeholders open a fullscreen viewer | [Usage](#usage) |
| Message history | Local SQLite cache, last 200 messages per contact retained; "Load more" fetches older cached messages | [Tips](#tips) |
| Device linking via QR code | Link new devices for Signal, WhatsApp, and Telegram directly from the TUI (`Ctrl+L`) or from helper scripts | [Device linking](#device-linking) |
| Delivery and read receipts | Sent / delivered / read states on outgoing bubbles, plus a distinct failed state; receipts persist across restarts (Telegram) | [Message delivery status](#message-delivery-status) |
| Typing indicators | See when a contact is typing, and briefly after they stop | [Tips](#tips) |
| Emoji picker | Category navigation, search, suggestions, and `:alias:` auto-completion while typing | [Emoji picker](#emoji-picker) |
| Download mode | Serve any message text or attachment via a temporary HTTP URL for easy download | [Download mode](#download-mode) |
| Contacts grouped per person | The same person across backends appears once as an expandable group; flat views when a single backend filter is active | [Contact grouping and unread filters](#contact-grouping-and-unread-filters) |
| Protocol and unread filters | Cycle backend filter (`Ctrl+W`), unread-only view (`Ctrl+U`), full view (`Ctrl+A`), clickable per-backend unread counters in the status bar | [Contact grouping and unread filters](#contact-grouping-and-unread-filters) |

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| **Python 3.10+** | Everything | |
| **Java 25 (JRE)** | Signal | Required by the `signal-cli` JVM build. Without it, Signal will not start. |
| **signal-cli** (JVM build) in `./bin/` | Signal | Downloaded automatically by `install.sh`; see [Installation](#installation). |
| **requirements-web.txt** | Web UI (optional) | Installed by default by install.sh (skip with --no-web). Never in requirements.txt by design. |
| **Docker + Docker Compose** | WhatsApp | Runs the WAHA container. Skip if you only use Signal. |
| **catimg** | Image rendering | Optional; falls back to text placeholders if missing. On kitty >= 0.20 images render natively, no `catimg` needed — see [Native inline images](#native-inline-images-kitty-graphics-protocol). |
| **A linked account** | Signal and/or WhatsApp and/or Telegram | Via `link_account.py` / `link_whatsapp.py` or the TUI `Ctrl+L` — see [Device linking](#device-linking). |

> **Note:** A Python virtual environment (venv) is **recommended** but not strictly required. See
> [Virtual environment](#virtual-environment-optional-but-recommended).

## Installation

### Option A — Automatic installation (recommended)

The easiest way is to use the provided `install.sh` script, which checks prerequisites, downloads the
correct `signal-cli` build, optionally starts the WAHA Docker container for WhatsApp, creates a
virtual environment and installs the Python dependencies:

```bash
git clone https://github.com/Bu3nd14/signal-tui-client.git
cd signal-tui-client
./install.sh
```

Supported options:

```bash
./install.sh --no-venv            # install without creating a virtual environment
./install.sh --version 0.14.7     # download a specific signal-cli version
./install.sh --skip-signal-cli    # skip downloading signal-cli (if already present)
./install.sh --update             # update signal-cli to the latest version
./install.sh --whatsapp           # start the WAHA Docker container for WhatsApp
./install.sh --check-whatsapp     # check WhatsApp prerequisites (Docker, ports, firewall)
./install.sh --no-web             # skip optional Web UI dependencies
./install.sh --aliases            # install only the Web UI shell aliases
./install.sh --help               # show usage
```

### Option B — Manual installation

#### 1. Clone the repository

```bash
git clone https://github.com/Bu3nd14/signal-tui-client.git
cd signal-tui-client
```

#### 2. Install Python dependencies

It is recommended to use a virtual environment (see
[Virtual environment](#virtual-environment-optional-but-recommended)):

```bash
pip install -r requirements.txt
```

#### 3. Download signal-cli

Download the **JVM build** of `signal-cli` (the full `signal-cli-X.Y.Z.tar.gz` archive, **not** the
`-Linux-client` or `-Linux-native` variants):

```bash
# Example for Linux x86_64 — replace X.Y.Z with the actual version (e.g. 0.14.7)
mkdir -p bin
cd bin
wget https://github.com/AsamK/signal-cli/releases/download/vX.Y.Z/signal-cli-X.Y.Z.tar.gz
tar xzf signal-cli-X.Y.Z.tar.gz
rm signal-cli-X.Y.Z.tar.gz
cd ..
```

> **Important — which build to download:**
>
> - **Use:** `signal-cli-X.Y.Z.tar.gz` — the **JVM build** (full archive with `bin/signal-cli` +
>   `lib/*.jar`). This is the **correct** one: it includes the `daemon` command (JSON-RPC over HTTP)
>   that this client uses, and it produces the `./bin/signal-cli-*/bin/signal-cli` structure the app
>   expects.
> - **Do not use:** `signal-cli-X.Y.Z-Linux-client.tar.gz` — only a **JSON-RPC client** (a single
>   native executable). It does **not** include the `daemon` command, so it cannot start the
>   JSON-RPC server this client needs.
> - **Do not use:** `signal-cli-X.Y.Z-Linux-native.tar.gz` — the GraalVM native build. It does not
>   produce the `./bin/signal-cli-*/bin/signal-cli` structure the app expects.
>
> The `releases/latest/download/` URL does **not** work with a versioned filename (the filename
> changes with each release). You must use the explicit `releases/download/vX.Y.Z/` URL and replace
> `X.Y.Z` with the actual version (e.g. `0.14.7`). The `install.sh` script resolves the latest
> version automatically.

The app will automatically find `signal-cli` in the `./bin/signal-cli-*/` directory.

#### 4. Install catimg (optional, for image rendering)

```bash
sudo apt install catimg
```

#### 5. Configure your phone number

Set your Signal phone number via environment variable:

```bash
export SIGNAL_USER_NUMBER="+1234567890"
```

Or create a `config.json` file in the project root:

```json
{
    "user_number": "+1234567890"
}
```

> **Note:** `config.json` is in `.gitignore` and will not be committed.

Next, optionally enable the extra backends:
[WhatsApp](#enabling-the-whatsapp-backend-optional) and/or
[Telegram](#enabling-the-telegram-backend-optional).

### Enabling the WhatsApp backend (optional)

WhatsApp is an **optional** backend that talks to a lightweight WhatsApp HTTP API.
The recommended way is to run the official **WAHA** (`devlikeapro/waha`) container
via Docker Compose — **no Node.js to install, no manual service to run**. If it
is not configured/started, the client runs exactly as before (Signal only) and
gracefully skips WhatsApp.

#### One-command startup (Docker)

```bash
docker compose up -d            # start WAHA (WhatsApp HTTP API) on http://127.0.0.1:3005
./scripts/start_whatsapp.sh     # (optional) start + wait until the API is ready
```

or use the installer:

```bash
./install.sh --whatsapp
```

The API then listens on `127.0.0.1:3005` by default (override with
`WHATSAPP_API_PORT`); session + media persist in `./whatsapp-data/` (git-ignored).
If Docker isn't installed, the configuration below still lets you point the
backend at any compatible Baileys API.

> **API key (authentication):** WAHA generates credentials on its **first** start
> and requires them afterwards — REST calls without the correct key return `401`.
> Copy the defaults and fill in the values the container printed/logged:
>
> ```bash
> cp .env.example .env       # then edit `.env` and set WAHA_API_KEY etc.
> docker compose up -d       # (re)start WAHA so it picks up the key
> ```
>
> `docker-compose.yml` loads `.env` via `env_file`, so WAHA reuses the key across
> restarts instead of regenerating it. The Python client reads the same
> `WAHA_API_KEY` from `.env` automatically; it can also be set explicitly with
> `WHATSAPP_API_KEY` (see below). To grab the current values from a running
> container: `docker exec signal-tui-whatsapp env | grep WAHA_API_KEY`.

#### Configuration (env or `config.json`)

Environment variables:

```bash
export WHATSAPP_API_PORT="3005"                       # docker-compose host port (default 3005)
export WHATSAPP_API_URL="http://127.0.0.1:3005"      # base URL of the WAHA API
export WHATSAPP_API_KEY="<api-key>"                  # X-Api-Key (auto-read from .env if unset)
export WHATSAPP_SESSION_NAME="default"               # session name (default "default")
export WHATSAPP_MEDIA_DIR="/srv/whatsapp-media"      # local media download dir
```

…or via `config.json`:

```json
{
    "user_number": "+1234567890",
    "whatsapp_api_url": "http://127.0.0.1:3005",
    "whatsapp_api_key": "<api-key>",
    "whatsapp_session_name": "default",
    "whatsapp_media_dir": "/srv/whatsapp-media"
}
```

#### Pair the device

With the API running, link the device with:

```bash
source .venv/bin/activate        # use the project venv (has qrcode + deps)
python3 link_whatsapp.py         # prints a QR to scan with WhatsApp
```

> Tip: `link_whatsapp.py` **auto-restarts** inside `.venv/bin/python` if you run
> it with the system python, so `python3 link_whatsapp.py` works either way.
>
> **Alternatively**, link directly from the TUI: launch the app and press `Ctrl+L`,
> select WhatsApp, and scan the QR code. The TUI handles the entire flow including
> waiting for server-side contacts sync with a progress indicator. See
> [Device linking](#device-linking).

### Enabling the Telegram backend (optional)

The Telegram backend uses [Telethon](https://docs.telethon.dev/) and requires
**no external daemon** — everything runs in-process.

#### Get API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number
2. Click **API Development Tools**
3. Fill in **App title** and **Short name** (any values are fine, no URL needed)
4. Click **Create application**
5. Copy your **`api_id`** (integer) and **`api_hash`** (string)

> Your `api_hash` is **secret** — never commit it or share it publicly.

#### Configure credentials

Add to the project `.env` file (`cp .env.example .env` if not already present):

```bash
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_hash_here
```

#### Pair the device (QR login)

With credentials configured, launch the TUI:

```bash
source .venv/bin/activate
python3 signal_tui.py
```

Press `Ctrl+L`, select **Telegram**, and scan the QR code with your phone.

> If your Telegram account has **2FA** enabled, the TUI will show a password
> field after scanning the QR. Enter your 2FA password and press Enter.
>
> If you don't have 2FA, the login completes automatically.

## Virtual environment (optional but recommended)

A Python virtual environment is **recommended** to avoid polluting your system Python and to prevent
dependency conflicts. It is **not strictly required** — the app is a standalone script launched with
`python3 signal_tui.py`.

> **Note:** On many Linux distributions, `pip install` at the system level is blocked (PEP 668 /
> "externally-managed-environment"). In that case a virtual environment is **required**.

```bash
# Create the virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

To deactivate the environment later, run `deactivate`. The `install.sh` script creates and uses the
virtual environment automatically (unless you pass `--no-venv`).

## Updating signal-cli

Signal's servers change over time, and `signal-cli` releases older than ~3 months may stop working.
To update `signal-cli` to the latest version, run:

```bash
./install.sh --update
```

This downloads the latest JVM build, extracts it into `./bin/`, and removes the previous version(s).

## Device linking

Before using the client, you need to link your accounts. You can do this either from the command
line or directly from the TUI.

### From the command line

```bash
python3 link_account.py          # Signal
python3 link_whatsapp.py         # WhatsApp
```

Each script displays a QR code. Scan it with the respective app on your phone. There is no CLI
script for Telegram — pair it from the TUI as described below.

### From the TUI

Launch the app and press `Ctrl+L`, select Signal, WhatsApp, or Telegram, then scan the QR code. For
Signal you may need to enter your phone number. For Telegram with 2FA, the TUI will prompt for your
password after scanning.

## Usage

```bash
python3 signal_tui.py
```

The app starts the `signal-cli` daemon automatically on first launch (for Signal) and a webhook
HTTP server for WhatsApp push events. See the subsections below for details on each area.

<a id="tips"></a>

### Controls

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate contact list |
| `Enter` | Select contact / open chat |
| `Enter` (on image) | Open image in fullscreen modal |
| `Escape` / `q` (in modal) | Close image modal |
| Type message + `Enter` | Send message (with quote if replying) |
| `Shift+Enter` / `Ctrl+Enter` / `Ctrl+J` | Insert a newline in the message input |
| `Click` / `Enter` (on message) | Select message to reply to |
| Close button (reply bar) | Cancel reply selection |
| `Alt+E` (or Alt+click, on your own text message) | Start editing that message — see [Message editing](#message-editing) |
| `Alt+R` (or Alt+click, on an image) | Quote that media in a reply — see [Replies and quoted media](#replies-and-quoted-media) |
| `Ctrl+E` | Open emoji picker |
| `Ctrl+S` | Open contact search over the full address book of all backends |
| `Ctrl+D` | Toggle download mode |
| `Ctrl+W` | Cycle contact filter: all → Signal → WhatsApp → Telegram (single-backend views are flat) |
| `Ctrl+U` | Toggle the "unread only" filter (combines with the `Ctrl+W` backend filter) |
| `Ctrl+A` | Return to the full **All** view (backend filter + unread filter off) |
| `Click` / `Enter` / `space` (on group header) | Expand / collapse a contact group (in the All view) |
| `Click` / `Enter` (on a row in a filtered view) | Open the chat directly |
| Click (on a status-bar segment) | Jump to that backend — unread view if it has unread, else its plain filter |
| `Ctrl+L` | Link a new device (Signal / WhatsApp / Telegram) via QR code |
| `Ctrl+N` / `Ctrl+P` | Navigate emoji suggestions / emoji picker categories |
| `Ctrl+Q` / `Ctrl+C` | Quit |

### Composing messages

The message input is a small multi-line editor:

- **`Enter`** sends the message.
- **`Shift+Enter`**, **`Ctrl+Enter`**, or **`Ctrl+J`** insert a newline, so you can write multi-line
  messages before sending.
- Pasted text with Windows/Mac line endings is normalized to `\n`.
- While typing, `:alias:` shortcuts are auto-completed (see [Emoji picker](#emoji-picker)).

### Emoji picker

Press **`Ctrl+E`** to open the emoji picker. Inside the picker:

- **`Tab`** / **`Shift+Tab`** — move focus between: category tabs → emoji grid → search bar
- **`Ctrl+F`** — jump to the search bar
- **`Ctrl+N`** / **`Ctrl+P`** — switch between emoji categories
- **`←`** / **`→`** / **`↑`** / **`↓`** — navigate the emoji grid
- **`Enter`** — insert the selected emoji at the cursor position
- **`Escape`** — close the picker without inserting

You can also type `:alias:` shortcuts directly in the message input — for example, typing
`:smile:` inserts a smiley face and `:heart:` inserts a heart symbol. A completion popup appears as
you type; use `Ctrl+N`/`Ctrl+P` to navigate and `Enter` to confirm.

### Contact search and address book

Press **`Ctrl+S`** to open the contact picker. Since the address-book update it searches the
**complete address book of all three backends** — not just contacts with an existing chat:

- **Type** — the result list updates live as you type, filtering by name or number (case-insensitive)
- **Tab** — move focus from the search input to the results list (`Shift+Tab` to go back)
- **↑** / **↓** — navigate the matching entries
- **Enter** / **click** — select the highlighted entry and close the picker
- **Escape** — close the picker without selecting
- **`Ctrl+W`** (inside the picker) — cycle the protocol filter: all → Signal → WhatsApp → Telegram

Behavior details:

- Contacts from the three backends are aggregated and sorted by recency, then alphabetically.
  The same person present on multiple protocols is shown as a single entry; selecting it opens a
  small sub-dialog to choose the backend (the most recent one is pre-selected; `Escape` returns to
  the picker).
- Selecting a contact **without an existing chat** opens-or-creates the conversation on that
  backend. Newly opened WhatsApp numbers get a best-effort existence check.
- The address book is loaded asynchronously; a loading indicator is shown while fetching.
- The selected contact's chat opens and is highlighted in the contact list, exactly as if you had
  selected it manually.

Implementation: `contact_picker.py`; design notes in `docs/DESIGN_CTRLS_RUBRICA.md`.

![Address book picker](assets/screenshots/address-book.png)
> **Screenshot placeholder** — capture with the real TUI and replace `address-book.png`

### Message editing

Your own **text** messages can be edited after sending, on all three protocols (Signal, Telegram,
WhatsApp):

- Press **`Alt+E`** (or **Alt+click**) on one of your own text messages: the text is loaded into the
  message input and the bubble is highlighted. Replying and editing are mutually exclusive —
  starting one cancels the other.
- **`Enter`** submits the edit **optimistically**: the bubble, in-memory cache, and database are
  updated immediately, then the network call runs in a worker thread. On failure or rejection the
  original text is restored automatically.
- The edited bubble keeps its original timestamp and gains a persistent `(modificato)` marker.
- **Incoming edits** from other people update the existing bubble **in place** — no duplicated
  messages, no unread bump. Edits you make from another linked device are applied to this session
  idempotently.

Limitations: only text messages can be edited (not media or captions); messages still *pending* or
*failed* cannot be edited; there is no edit history — the latest text wins.

Implementation: `tui/edit.py`; design notes in `docs/DESIGN_EDIT_MESSAGES.md`.

![Editing a sent message](assets/screenshots/message-editing.png)
> **Screenshot placeholder** — capture with the real TUI and replace `message-editing.png`

### Replies and quoted media

- **Reply to a text message**: click it (or focus and press `Enter`) — the reply bar shows what you
  are quoting; the close button cancels.
- **Quote an image in a reply**: press **`Alt+R`** or **Alt+click** on an image. A plain click /
  `Enter` still opens the fullscreen viewer, unchanged.
- **Incoming media quotes are visible**: when someone replies quoting a photo/video/audio/file, the
  quoted bubble shows the real caption when present, otherwise a typed media label.
- **Outgoing media quotes reach recipients correctly**: on Signal the quoted thumbnail travels with
  the message (`quoteAttachments`), so the recipient sees the quoted image; WhatsApp and Telegram
  use their native reply IDs.

Retrying a failed media reply preserves the correct quote even after a restart.

Implementation: `ui_components.py` (`alt+r` binding on `ImageWidget`), `tui/send.py`;
design notes in `docs/DESIGN_QUOTE_MEDIA_37_V2.md` and `docs/DESIGN_QUOTE_MEDIA_37_PLANB.md`.

![Replying with a quoted image](assets/screenshots/quote-media.png)
> **Screenshot placeholder** — capture with the real TUI and replace `quote-media.png`

### Message delivery status

Outgoing messages show their delivery status directly on the bubble:

| Status | Style | Meaning |
|---|---|---|
| *pending* | dim/muted color | Being sent (optimistic phase) |
| *sent* | italic | Accepted by the server, not yet delivered |
| *delivered* | bold | Reached the recipient's device |
| read | normal | Seen by the recipient |
| ***failed*** | bold, red | Send rejected or errored — the message was not delivered |

Notes:

- The *pending → sent* transition is robust against timestamp differences in the daemon's echo of
  your own message (no stuck grey bubble).
- Failed sends remain marked as such so nothing is silently lost.
- Right after you send a message, the contact list **re-sorts immediately by recency** — the chat
  you just wrote to jumps to the top without waiting for the server round trip.
- Read receipts work on all three protocols: WhatsApp runtime receipts are reflected live, and
  Telegram read state persists across restarts.

### Download mode

Press **`Ctrl+D`** to enter download mode, then click any message to serve it for download:

- **Text messages** are served as `.txt` files
- **Images and attachments** are served with their original filename and extension

A persistent HTTP server starts on port **10042** (first download only) and stays alive for the
duration of the app. The download URL is shown in a selectable `Input` widget — use **Tab** to focus
it, then **Cmd+C / Ctrl+C** to copy the URL and paste it into your browser.

> **Note:** You need to open port **10042** on your server's firewall for downloads to work.

> **macOS users:** Terminal.app does not allow copying text from Textual widgets with `Cmd+C`. For
> the best experience, use **[iTerm2](https://iterm2.com/)** (free), which supports clipboard access
> from terminal applications.

### Contact grouping and unread filters

The contact list groups the same person across backends: one **header** row shows the person's best
name plus an aggregate unread badge (in the "All" view), and one **member** row per protocol sits
below it, labeled with that protocol's icon and color.

- Groups are **collapsed by default**: you see one row per person. `Click`/`Enter`/`space` on the
  header expands or collapses the group; `Click`/`Enter` on a **member** row opens that backend's
  chat.
- In **"All"** view the header badge is the aggregate unread count of the person's members.
- With a **single-backend filter** (`Ctrl+W`) the list becomes **flat**: one row per person (no
  chevron), clicking a row opens the chat directly, and the badge shows only the unread of that
  filtered view. Rows are sorted by per-backend recency and the first contact is auto-selected when
  you change filter. The backend border color is kept even when the unread filter is on.
- **`Ctrl+U`** toggles the **unread-only** filter: only contacts/groups with at least one unread
  message are shown. It composes with `Ctrl+W` (e.g. `Ctrl+W` → WhatsApp, then `Ctrl+U` → only your
  unread WhatsApp messages). The currently selected contact stays pinned in view.
- **`Ctrl+A`** returns to the full All view (both filters off).
- The **status bar** always shows the per-backend unread totals as three clickable segments (one per
  protocol, `-` when zero). Clicking a segment jumps to that backend: to its **unread view** if it
  has unread messages, otherwise to its **plain filter**. When the bar shows a transient message or
  an error the segments are hidden and reappear (updated) once the message clears.

> Note: `Ctrl+U` and `Ctrl+A` take over two text-editing shortcuts in the message input
> ("delete to line start", "cursor to line start"); use `super+backspace` / `home` instead.
> While the contact picker (`Ctrl+S`) is open these shortcuts are disabled.

### Tips

- Messages are persisted locally in a SQLite database
  (`~/.local/share/signal-tui-client/messages.db`) with up to 200 messages retained per contact
- The last 20 messages are shown when opening a chat; click "Load more" to see older cached messages
- Unread messages are shown with a `*N` badge: aggregate on the group header in the "All" view,
  filtered to the current backend in filtered views
- A writing-hand icon next to a contact name means they are typing; a thought-bubble icon appears
  briefly after they send a message or stop typing
- Use `Ctrl+U` to focus on unread conversations, `Ctrl+A` to go back to All — see
  [Contact grouping and unread filters](#contact-grouping-and-unread-filters)

## Web reader aliases

Three shell aliases (bash/zsh) launch the optional web reader and manage its lifecycle:

| Alias | What it does |
|---|---|
| `web-signal-tui` | Starts the TUI with the web server in the **foreground** on `0.0.0.0:4242` |
| `web-signal-tui-bg` | Starts the TUI + web server in a **detached tmux session** (background) and **exports** the Bearer token to your shell as `SIGNAL_TUI_WEB_TOKEN` |
| `web-signal-tui-stop` | Cleanly stops the tmux session and removes `/tmp/signal-tui.lock` |

> The web server requires the optional dependencies in requirements-web.txt (installed by default; if you used --no-web: .venv/bin/pip install -r requirements-web.txt). Without them the TUI logs "optional dependencies are missing (web down)" and continues normally.

### Fast cycle (background + token)

`web-signal-tui-bg` runs in the background and exports `SIGNAL_TUI_WEB_TOKEN` to the current shell,
so the token is immediately ready for the Web UI login (it is printed to the console) or for `curl`:

```bash
web-signal-tui-bg
# TUI bg avviata — token: <the-token>

curl -H "Authorization: Bearer $SIGNAL_TUI_WEB_TOKEN" http://127.0.0.1:4242/api/contacts
```

The Bearer token lives in `config.json` under `web.token`; the web server also accepts it via the
`SIGNAL_TUI_WEB_TOKEN` environment variable. The default port is `4242`.

### Installing the aliases

Automatic install — detects bash/zsh and writes the real project path:

```bash
./install.sh --aliases
```

Or manually copy the alias block from [docs/ALIASES.md](docs/ALIASES.md) into `~/.bashrc` (bash) or
`~/.zshrc` (zsh), then `source ~/.bashrc` (or reopen the shell).

> **Compatibility:** bash and zsh are supported; other shells (fish, dash/sh) are not — see
> [docs/ALIASES.md](docs/ALIASES.md).

## Native inline images (kitty graphics protocol)

In chats with images the client renders **high-resolution inline thumbnails** directly in the
conversation and opens the viewer as a **hi-res modal**, when the terminal supports the *kitty
graphics protocol*. Everywhere else the classic behavior applies: clickable placeholder + `catimg`
in the modal.

### Requirements (native mode)

- **[kitty](https://sw.kovidgoyal.net/kitty/) >= 0.20** terminal (developed and tested on kitty 0.48)
- Client launched in a **direct** terminal or over **ssh** (does not work inside tmux/screen)
- `pillow>=10.3` — already included in `requirements.txt`

> Detection runs once at startup (`TERM=xterm-kitty` + a graphics-protocol query); any error
> degrades gracefully to the `catimg` fallback without blocking the app.

### Behavior per environment

| Environment | Mode | What happens |
|---|---|---|
| kitty, direct or ssh (`TERM=xterm-kitty`) | **kitty** | High-resolution inline thumbnails + hi-res modal (1600 px cap on the long side); smooth scrolling without retransmissions; images never overlap pickers/modals |
| Other terminals (Ghostty, iTerm2, Windows Terminal, xterm…) | `catimg` | Experience unchanged: placeholder + `catimg` modal |
| tmux / GNU screen (even inside kitty) | `catimg` | Image passthrough is unreliable → always falls back |
| Pipe / CI / headless shell (non-tty) | `catimg` | No terminal query → test suites and CI stay safe |
| Unsupported terminal and `catimg` missing | `off` | Placeholder only; clicking shows a "rendering disabled" status, no modal |

![Native kitty thumbnails](assets/screenshots/native-images-kitty.png)
> **Screenshot placeholder** — capture with the real TUI and replace `native-images-kitty.png`

### Configuration

The mode is automatic (`auto`). To force it, use the `IMAGE_PROTOCOL` environment variable or the
`image_protocol` key in `config.json`:

```bash
export IMAGE_PROTOCOL=auto    # auto | kitty | catimg | off
```

```json
{
    "image_protocol": "auto"
}
```

Thumbnail limits are configurable the same way (env or `config.json`):

| `config.json` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `thumbnail_max_lines` | `THUMBNAIL_MAX_LINES` | `12` | Maximum thumbnail height, in rows |
| `thumbnail_max_cols` | `THUMBNAIL_MAX_COLS` | `60` | Maximum width, in columns (also clamped to the chat width) |

> **Developer note:** the client is normally launched with the **`signal`** alias (DEBUG log on
> `/tmp/signal-tui.log`; the app also accepts a `--debug` flag for verbose logging). Manual
> validation on a real kitty follows the **local** checklist `docs/CHECKLIST_MANUAL_KITTY.md`
> (gitignored, not distributed with the repo). Technical details (protocol handling, clipping,
> screen-stack management) are in `documentation/design/DESIGN_NATIVE_IMAGES.md`.

## Performance profiling

To profile the application in terms of **CPU**, **RAM**, and **I/O**, use the tools in the
`profiling/` folder:

```bash
# Resource monitoring (CPU, RAM, I/O over time)
python profiling/monitor_resources.py --duration 120
python profiling/analyze_resources.py

# CPU flamegraph (py-spy — samples all threads)
./profiling/run_pyspy.sh 120

# RAM profiling (tracemalloc)
python profiling/profile_memory.py --duration 120

# I/O profiling (strace)
./profiling/run_strace.sh 120
```

See [profiling/README.md](profiling/README.md) for detailed instructions, result interpretation,
and known hotspots.

Notable performance work: UI freezes on message arrival/send were eliminated (all blocking work
runs in worker threads), and the WhatsApp presence subscription was disabled to cut idle load and
speed up outgoing bubbles.

## Testing

### Standard suite (unit + integration, CI-safe)

Runs every test in `tests/` and `Telegram/` (no network, no real accounts):

```bash
make test PYTHON=.venv-test/bin/python      # or: source .venv-test/bin/activate && make test
make lint                                   # ruff check
make coverage                               # test + coverage (gate 68%)
```

This suite also runs in CI (`.github/workflows/ci.yml`, Python 3.12/3.13).

### Optional "live" integration tests (real accounts)

The tests in `tests/test_live_quote_media.py` (E1–E7) verify end-to-end behavior **against a real
test account** (present on all three protocols) and **send real messages**. They never run in CI:
without `LIVE_TESTS=1` they are always skipped.

**Prerequisites:**

- signal-cli daemon running (`config.json` with `user_number`) — required for Signal;
- WAHA reachable with a WORKING session (for WhatsApp);
- `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` + an authorized Telethon session (for Telegram);
- the test contact present on all three protocols, or explicit ID overrides with
  `LIVE_TARGET_SIGNAL` / `LIVE_TARGET_WHATSAPP` / `LIVE_TARGET_TELEGRAM`.

**Run them:**

```bash
make live-test PYTHON=.venv-test/bin/python
# equivalent to: LIVE_TESTS=1 .venv-test/bin/python -m pytest tests/test_live_quote_media.py -v
```

Coverage (criteria §10.2 of `docs/DESIGN_QUOTE_MEDIA_37_V2.md`):

- **E1/E2/E3/E7** — Signal: media quote with/without caption (`quoteMessage`/`quoteAttachments`
  verified on the wire), plain text reply unchanged, retry after failure;
- **E5** — WhatsApp: photo quote (`reply_to` = Baileys ID);
- **E6** — Telegram: photo quote (`reply_to` = numeric ID).

If the chat has no fresh media with a persisted `content_type` (legacy media predating plan B is
not auto-backfilled — use `migrate_content_type.py`), the Signal tests print a reminder to send a
new image and wait up to 90 seconds: just send a photo from the official client of the test contact
and the test proceeds on its own.

**E4 (ingress, manual):** requires quoting an image from the official client of the test contact
while the test waits:

```bash
make live-test-manual PYTHON=.venv-test/bin/python
```

**Notes:** every test sends messages tagged `[live-test]` (recognizable on the device). Tests skip
with clear messages when a backend is unconfigured, the contact cannot be resolved, or no suitable
media exists — they never fail the suite.

## Project structure

```
signal-tui-client/
├── signal_tui.py              # Entry point — main TUI application (Textual App), multi-protocol
├── backend/                   # Shared backend: SQLite persistence, signal-cli RPC/subprocess,
│                              #   webhook/HTTP server, receipts
├── backends/                  # Per-protocol backend implementations
│   ├── base.py                #   Abstract ChatBackend interface (+ address book, edit contracts)
│   ├── manager.py             #   Multi-backend registry and routing
│   ├── signal.py              #   Signal backend (signal-cli daemon / subprocess, envelope parsing)
│   ├── whatsapp.py            #   WhatsApp backend facade
│   ├── whatsapp_events.py     #   WhatsApp webhook event ingestion
│   ├── whatsapp_rest.py       #   WhatsApp REST calls (WAHA)
│   ├── telegram.py            #   Telegram backend (Telethon MTProto, QR login, read receipts)
│   └── config.py              #   WhatsApp + Telegram configuration helpers
├── tui/                       # TUI composition: mixins split by concern
│   ├── app.py                 #   App wiring, bindings, layout
│   ├── contacts.py            #   Contact list logic (grouping, filters, open-or-create)
│   ├── chat_view.py           #   Chat rendering (bubbles, images, thumbnails)
│   ├── events.py              #   Event handling (messages, receipts, typing, edits)
│   ├── send.py                #   Optimistic send flow + retries
│   ├── edit.py                #   Message editing flow (optimistic submit, rollback)
│   ├── unread_reply.py        #   Reply/edit bars and handlers
│   ├── download.py            #   Download mode HTTP server glue
│   ├── pickers.py             #   Modal screens (emoji/contact/device-link launching)
│   ├── polling.py             #   Timed refreshes
│   ├── backend_connect.py     #   Backend startup/connection
│   ├── css.py                 #   TUI stylesheet
│   └── images/                #   Native kitty rendering: detect.py, cellsize.py, kitty_renderer.py
├── models.py                  # Shared data models (ChatContact, ChatMessage, ChatEvent)
├── ui_components.py           # Custom Textual widgets (MessageWidget, ImageWidget, StatusBar,
│                              #   ImageModalScreen, MessageTextArea, …)
├── emoji_picker.py            # Emoji picker modal screen and auto-completion widget (Ctrl+E)
├── emoji_data.py              # Emoji database (categories, aliases, search index)
├── contact_picker.py          # Address-book contact picker modal screen (Ctrl+S)
├── device_link_screen.py      # Device link picker (Signal / WhatsApp / Telegram QR pairing)
├── qr_utils.py                # QR code renderer (ASCII / PNG-to-ASCII)
├── link_account.py            # Signal device linking script (QR code — or use Ctrl+L in TUI)
├── link_whatsapp.py           # WhatsApp device linking script (QR code — or use Ctrl+L in TUI)
├── migrate_cache_sqlite.py    # One-shot migration: JSON cache → SQLite
├── migrate_cache_protocol.py  # One-shot migration: add protocol field to cache
├── migrate_cache_status.py    # One-shot migration: add status field to cache
├── migrate_content_type.py    # Backfill MIME type for legacy cached media (quote media plan B)
├── purge_whatsapp_cache.py    # Utility: purge WhatsApp messages from cache
├── Telegram/                  # Telegram test suite
│   ├── test_telegram_backend.py
│   └── test_regression.py
├── tests/                     # Test suite (pytest; unit, UI pilot, integration, live-gated)
│   ├── conftest.py
│   ├── test_live_quote_media.py   # Live E2E tests (opt-in via LIVE_TESTS=1)
│   └── …                      # Backend, UI, grouping, filters, edit flow, quote media, …
├── profiling/                 # Performance profiling tools (CPU, RAM, I/O)
├── scripts/                   # Helper scripts (start_whatsapp.sh, dump_address_book_fixtures.py)
├── documentation/             # Generated technical docs (architecture, design, API contracts,
│                              #   test suite, review)
├── docs/                      # Working docs: BUGS.md, TEST_REPORT.md, PERF_ANALYSIS.md,
│                              #   DESIGN_*.md, PLAN_*.md
├── assets/screenshots/        # README screenshots (main UI, image viewer)
├── docker-compose.yml         # WAHA (WhatsApp HTTP API) Docker container
├── .env.example               # Template for WAHA + Telegram credentials
├── install.sh                 # Automatic installation script
├── Makefile                   # Shared commands: make test / lint / coverage / live-test
├── pyproject.toml             # Shared pytest / coverage / ruff config
├── .github/workflows/ci.yml   # CI: lint + test (3.12/3.13 matrix) + coverage gate + Codecov
├── requirements.txt           # Python dependencies (textual, telethon, qrcode, ...)
├── requirements-dev.txt       # Development dependencies (pytest, pytest-cov, coverage, ruff)
├── config.json                # Local configuration (not committed)
├── README.md                  # This file
├── bin/                       # signal-cli binaries (not committed)
└── LICENSE                    # GPLv3
```

Current test-suite status is tracked in [docs/TEST_REPORT.md](docs/TEST_REPORT.md); known bugs and
limitations in [docs/BUGS.md](docs/BUGS.md).

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
