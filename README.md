# Signal / WhatsApp / Telegram TUI Client

[![codecov](https://codecov.io/gh/Bu3nd14/signal-tui-client/graph/badge.svg)](https://codecov.io/gh/Bu3nd14/signal-tui-client)

A terminal-based (TUI) multi-protocol client built with [Textual](https://textual.textualize.io/).

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

## Features

- Full contact list with unread badges — unified across Signal, WhatsApp, and Telegram
- Real-time message receiving and sending on all three protocols
- **Multiple attachments** — a message with several photos shows each one separately (both Signal and WhatsApp).  Clickable image placeholders with fullscreen viewer (via catimg).
- **Native inline images on kitty** — high-resolution inline thumbnails and a fullscreen hi-res viewer via the kitty graphics protocol (kitty ≥ 0.20, direct terminal or ssh); automatic `catimg` fallback everywhere else.  See [Immagini native inline](#immagini-native-inline-kitty-graphics-protocol).
- Message history with local SQLite cache (last 200 messages per contact retained)
- Device linking via QR code — Signal, WhatsApp, and Telegram
- Daemon mode for fast JSON-RPC communication (Signal)
- WhatsApp event-driven mode — webhook-based, no polling (WAHA + Docker)
- Telegram native mode — MTProto event loop in dedicated thread (Telethon)
- Automatic fallback to subprocess if daemon is not running (Signal)
- Reply to messages — click any message to quote it in your reply
- Emoji picker (`Ctrl+E`) with category navigation, search, and `:alias:` auto-completion
- Contact search (`Ctrl+S`) — search contacts by name or number with a live-updating picker
- Download mode (`Ctrl+D`) — serve message text or attachments via temporary HTTP server for download
- Device linking (`Ctrl+L`) — link a new Signal, WhatsApp, or Telegram device directly from the TUI with QR code scanning
- Unified multi-protocol contact list with `Ctrl+W` cycle filter: all → Signal → WhatsApp → Telegram
- **Contacts grouped per person** — the same person on Signal + WhatsApp + Telegram appears once, as an expandable group; groups are **collapsed by default** (click/`Enter`/`space` on the header expands/collapses them)
- **Per-backend unread counters in the status bar** — `📱 N  💬 N  📨 N` (total unread per protocol, `-` when zero), always visible even when a filter hides those contacts; **click an icon** to jump straight to that backend (its unread view if it has unread, otherwise its plain filter)
- **Unread-only filter (`Ctrl+U`)** — show only contacts with unread messages; combines with the `Ctrl+W` protocol filter (`Ctrl+A` returns to the full **All** view)
- **Flat filtered views** — when a single backend filter is active the list becomes flat (one row per person, no expand): clicking a row opens the chat directly
- Protocol-aware theming — 📱 Signal, 💬 WhatsApp, 📨 Telegram with distinct emoji labels
- Message delivery and read receipts — sent messages show status: *sent* (italic), **delivered** (bold), read (normal).  Works for all three protocols.
- Typing indicators — see when a contact is typing (✍️ icon next to their name); a 💭 icon shows briefly after they stop typing or send a message.  Works for Signal and WhatsApp.




## Prerequisites

- **Python 3.10+**
- **Java 25 (JRE)** — required by `signal-cli` (the JVM build). Without it, Signal will not start.
- **signal-cli** — download and place in `./bin/` directory (see Installation).  Required for the **Signal** backend only.
- **Docker + Docker Compose** — required for the **WhatsApp** backend (runs the WAHA container).
  Skip this if you only use Signal.
- **catimg** — for rendering images in the terminal (optional; falls back to text placeholder if missing).  On **kitty ≥ 0.20** images render natively, no `catimg` needed — see [Immagini native inline](#immagini-native-inline-kitty-graphics-protocol).
- A linked account — Signal (via `link_account.py` or TUI `Ctrl+L`) and/or WhatsApp (via `link_whatsapp.py` or TUI `Ctrl+L`)

> **Note:** A Python virtual environment (venv) is **recommended** but not strictly required. See [Virtual environment](#virtual-environment-optional-but-recommended).

## Installation

### Option A — Automatic installation (recommended)

The easiest way is to use the provided `install.sh` script, which checks prerequisites, downloads the correct `signal-cli` build, optionally starts the WAHA Docker container for WhatsApp, creates a virtual environment and installs the Python dependencies:

```bash
git clone https://github.com/Bu3nd14/signal-tui-client.git
cd signal-tui-client
./install.sh
```

The script supports several options:

```bash
./install.sh --no-venv            # install without creating a virtual environment
./install.sh --version 0.14.7     # download a specific signal-cli version
./install.sh --skip-signal-cli    # skip downloading signal-cli (if already present)
./install.sh --update             # update signal-cli to the latest version
./install.sh --whatsapp           # start the WAHA Docker container for WhatsApp
./install.sh --check-whatsapp     # check WhatsApp prerequisites (Docker, ports, firewall)
./install.sh --help               # show usage
```

### Option B — Manual installation

#### 1. Clone the repository

```bash
git clone https://github.com/Bu3nd14/signal-tui-client.git
cd signal-tui-client
```

#### 2. Install Python dependencies

It is recommended to use a virtual environment (see [Virtual environment](#virtual-environment-optional-but-recommended)):

```bash
pip install -r requirements.txt
```

#### 3. Download signal-cli

Download the **JVM build** of `signal-cli` (the full `signal-cli-X.Y.Z.tar.gz` archive, **not** the `-Linux-client` or `-Linux-native` variants):

```bash
# Example for Linux x86_64 — replace X.Y.Z with the actual version (e.g. 0.14.7)
mkdir -p bin
cd bin
wget https://github.com/AsamK/signal-cli/releases/download/vX.Y.Z/signal-cli-X.Y.Z.tar.gz
tar xzf signal-cli-X.Y.Z.tar.gz
rm signal-cli-X.Y.Z.tar.gz
cd ..
```

> **⚠️ Important — which build to download:**
> - ✅ **`signal-cli-X.Y.Z.tar.gz`** — the **JVM build** (full archive with `bin/signal-cli` + `lib/*.jar`). This is the **correct** one: it includes the `daemon` command (JSON-RPC over HTTP) that this client uses, and it produces the `./bin/signal-cli-*/bin/signal-cli` structure the app expects.
> - ❌ **`signal-cli-X.Y.Z-Linux-client.tar.gz`** — this is only a **JSON-RPC client** (a single native executable). It does **not** include the `daemon` command, so it cannot start the JSON-RPC server this client needs. **Do not use it.**
> - ❌ **`signal-cli-X.Y.Z-Linux-native.tar.gz`** — the GraalVM native build. It does not produce the `./bin/signal-cli-*/bin/signal-cli` structure the app expects. **Do not use it.**
>
> The `releases/latest/download/` URL does **not** work with a versioned filename (the filename changes with each release). You must use the explicit `releases/download/vX.Y.Z/` URL and replace `X.Y.Z` with the actual version (e.g. `0.14.7`). The `install.sh` script resolves the latest version automatically.

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

#### 6. (Optional) Enable the WhatsApp backend

WhatsApp is an **optional** backend that talks to a lightweight WhatsApp HTTP API.
The recommended way is to run the official **WAHA** (`devlikeapro/waha`) container
via Docker Compose — **no Node.js to install, no manual service to run**.  If it
is not configured/started, the client runs exactly as before (Signal only) and
gracefully skips WhatsApp.

##### 6a. One-command startup (Docker)

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
If Docker isn't installed, the instructions in section 6b below still let you
point the backend at any compatible Baileys API.

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
> restarts instead of regenerating it.  The Python client reads the same
> `WAHA_API_KEY` from `.env` automatically; it can also be set explicitly with
> `WHATSAPP_API_KEY` (see 6b).  To grab the current values from a running
> container: `docker exec signal-tui-whatsapp env | grep WAHA_API_KEY`.

##### 6b. Configuration (env or `config.json`)

Env variables:

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

##### 6c. Pair the device

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
> waiting for server-side contacts sync with a progress indicator.


#### 7. (Optional) Enable the Telegram backend

The Telegram backend uses [Telethon](https://docs.telethon.dev/) and requires
**no external daemon** — everything runs in-process.

##### 7a. Get API credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number
2. Click **API Development Tools**
3. Fill in **App title** and **Short name** (any values are fine, no URL needed)
4. Click **Create application**
5. Copy your **`api_id`** (integer) and **`api_hash`** (string)

> ⚠️ Your `api_hash` is **secret** — never commit it or share it publicly.

##### 7b. Configure credentials

Add to the project `.env` file (`cp .env.example .env` if not already present):

```bash
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_hash_here
```

##### 7c. Pair the device (QR login)

With credentials configured, launch the TUI:

```bash
source .venv/bin/activate
python3 signal_tui.py
```

Press `Ctrl+L`, select **📨 Telegram**, and scan the QR code with your phone.

> If your Telegram account has **2FA** enabled, the TUI will show a password
> field after scanning the QR. Enter your 2FA password and press Enter.
>
> If you don't have 2FA, the login completes automatically.


## Virtual environment (optional but recommended)

A Python virtual environment is **recommended** to avoid polluting your system Python and to prevent dependency conflicts. It is **not strictly required** — the app is a standalone script launched with `python3 signal_tui.py`.

> **Note:** On many Linux distributions, `pip install` at the system level is blocked (PEP 668 / "externally-managed-environment"). In that case a virtual environment is **required**.

```bash
# Create the virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

To deactivate the environment later, run `deactivate`. The `install.sh` script creates and uses the virtual environment automatically (unless you pass `--no-venv`).

## Updating signal-cli

Signal's servers change over time, and `signal-cli` releases older than ~3 months may stop working. To update `signal-cli` to the latest version, run:

```bash
./install.sh --update
```

This downloads the latest JVM build, extracts it into `./bin/`, and removes the previous version(s).

## Device Linking

Before using the client, you need to link your accounts. You can do this
either from the command line or directly from the TUI.

### From the command line

```bash
python3 link_account.py          # Signal
python3 link_whatsapp.py         # WhatsApp
python3 link_telegram.py         # Telegram (if script exists)
```

Each script displays a QR code. Scan it with the respective app on your phone.

### From the TUI

Launch the app and press `Ctrl+L`, select Signal, WhatsApp, or Telegram, then
scan the QR code. For Signal you may need to enter your phone number. For
Telegram with 2FA, the TUI will prompt for your password after scanning.

## Usage

```bash
python3 signal_tui.py
```

### Controls

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate contact list |
| `Enter` | Select contact / open chat |
| `Enter` (on image) | Open image in fullscreen modal |
| `Escape` / `q` (in modal) | Close image modal |
| `Click` / `Enter` (on message) | Select message to reply to |
| `✕` button | Cancel reply selection |
| Type message + `Enter` | Send message (with quote if replying) |
| `Ctrl+E` | Open emoji picker |
| `Ctrl+S` | Open contact search picker |
| `Ctrl+D` | Toggle download mode |
| `Ctrl+W` | Cycle contact filter: all → Signal → WhatsApp → Telegram (single-backend views are flat) |
| `Ctrl+U` | Toggle the "unread only" filter (combines with the `Ctrl+W` backend filter) |
| `Ctrl+A` | Return to the full **All** view (backend filter + unread filter off) |
| `Click` / `Enter` (on group header) | Expand / collapse a contact group (in the All view) |
| `space` (on group header) | Expand / collapse a contact group |
| `Click` / `Enter` (on a row in a filtered view) | Open the chat directly |
| `Click` (on a status-bar icon) | Jump to that backend — unread view if it has unread, else its plain filter |
| `Ctrl+L` | Link a new device (Signal / WhatsApp / Telegram) via QR code |
| `Ctrl+N` / `Ctrl+P` | Navigate emoji suggestions / emoji picker categories |
| `Ctrl+Q` | Quit |
| `Ctrl+C` | Quit |


### Emoji

Press **`Ctrl+E`** to open the emoji picker. Inside the picker:

- **`Tab`** / **`Shift+Tab`** — move focus between: category tabs → emoji grid → search bar
- **`Ctrl+N`** / **`Ctrl+P`** — switch between emoji categories
- **`←`** / **`→`** / **`↑`** / **`↓`** — navigate the emoji grid
- **`Enter`** — insert the selected emoji at the cursor position
- **`Escape`** — close the picker without inserting

You can also type `:alias:` shortcuts directly in the message input (e.g. `:smile:` → 😊, `:heart:` → ❤️). A completion popup will appear as you type; use `Ctrl+N`/`Ctrl+P` to navigate and `Enter` to confirm.

### Contact Search

Press **`Ctrl+S`** to open the contact search picker. Inside the picker:

- **Type** — the result list updates live as you type, filtering contacts by name or number (case-insensitive)
- **`Tab`** — move focus from the search input to the results list (`Shift+Tab` to go back)
- **`↑`** / **`↓`** — navigate the list of matching contacts
- **`Enter`** — select the highlighted contact, open its chat, and close the picker
- **Click** — select a contact directly with the mouse
- **`Escape`** — close the picker without selecting

Selecting a contact from the picker opens its chat **and** highlights it in the contact list on the left, exactly as if you had selected it manually.


### Download Mode


Press **`Ctrl+D`** to enter download mode, then click any message to serve it for download:

- **Text messages** are served as `.txt` files
- **Images and attachments** are served with their original filename and extension

A persistent HTTP server starts on port **10042** (first download only) and stays alive for the duration of the app. The download URL is shown in a selectable `Input` widget — use **Tab** to focus it, then **Cmd+C / Ctrl+C** to copy the URL and paste it into your browser.

> **Note:** You need to open port **10042** on your server's firewall for downloads to work.

> **macOS users:** The download URL is shown in a selectable `Input` widget, but **Terminal.app** does not allow copying text from Textual widgets with `Cmd+C`. For the best experience, use **[iTerm2](https://iterm2.com/)** (free) which supports clipboard access from terminal applications.

### Contact Grouping & Unread Filters

The contact list groups the same person across backends: one **header** row shows the
person's best name plus an aggregate unread badge (in the "All" view), and one
**member** row per protocol (`📱 Signal`, `💬 WhatsApp`, `📨 Telegram`) sits below it.

- Groups are **collapsed by default**: you see one row per person. `Click`/`Enter`/`space`
  on the header expands or collapses the group; `Click`/`Enter` on a **member** row opens
  that backend's chat.
- In **"All"** view the header badge is the aggregate unread count of the person's members.
- With a **single-backend filter** (`Ctrl+W`) the list becomes **flat**: one row per person
  (no chevron), clicking a row opens the chat directly, and the badge shows only the unread
  of that filtered view. The backend border color is kept even when the unread filter is on.
- **`Ctrl+U`** toggles the **unread-only** filter: only contacts/groups with at least one
  unread message are shown. It composes with `Ctrl+W` (e.g. `Ctrl+W` → WhatsApp, `Ctrl+U` →
  only your unread WhatsApp messages).
- **`Ctrl+A`** returns to the full All view (both filters off).
- The **status bar** always shows the per-backend unread totals (`📱 N  💬 N  📨 N`, `-` when
  zero). Clicking an icon jumps to that backend: to its **unread view** if it has unread
  messages, otherwise to its **plain filter**. When the bar shows a transient message or an
  error the icons are hidden and reappear (updated) once the message clears.

> Note: `Ctrl+U` and `Ctrl+A` take over two text-editing shortcuts in the message input
> ("delete to line start", "cursor to line start"); use `super+backspace` / `home` instead.
> While the contact search picker (`Ctrl+S`) is open the new shortcuts are disabled.

### Tips

- The app starts the `signal-cli` daemon automatically on first launch (for Signal) and a webhook HTTP server for WhatsApp push events
- Messages are persisted locally in a SQLite database (`~/.local/share/signal-tui-client/messages.db`) with up to 200 messages retained per contact
- The last 20 messages are shown when opening a chat; click "Load more" to see older cached messages
- Unread messages are shown with a `*N` badge: aggregate on the group header in the "All" view, filtered to the current backend in filtered views
- The contact list is **grouped per person and sorted by recency** — typing, mumbling and unread states are shown as icons/badges; use `Ctrl+U` to focus on unread, `Ctrl+A` to go back to All
- Sent messages show their delivery status: *sent* (italic), **delivered** (bold), read (normal)
- A `✍️` icon next to a contact name means they are typing; a `💭` icon shows briefly after they send the message or stop typing
- The contact list is **grouped per person and sorted by recency** — typing, mumbling and unread states are shown as icons/badges; use `Ctrl+U` to focus on unread, `Ctrl+A` to go back to All



## Immagini native inline (kitty graphics protocol)

Nei messaggi con immagini il client mostra **miniature inline ad alta risoluzione** direttamente nella chat e apre il visualizzatore in un **modal hi-res**, quando il terminale supporta il *kitty graphics protocol*.  Negli altri ambienti resta il comportamento classico: placeholder cliccabile + `catimg` nel modal.

### Requisiti (modalità nativa)

- Terminale **[kitty](https://sw.kovidgoyal.net/kitty/) ≥ 0.20** (sviluppo e test su kitty 0.48)
- Client lanciato in un terminale **diretto** o via **ssh** (dentro tmux/screen non funziona)
- `pillow>=10.3` — già inclusa in `requirements.txt`

> La detection gira una sola volta all'avvio (`TERM=xterm-kitty` + query al protocollo grafico); qualsiasi errore degrada al fallback `catimg` senza bloccare l'app.

### Comportamento per ambiente

| Ambiente | Modalità | Cosa succede |
|---|---|---|
| kitty, diretto o via ssh (`TERM=xterm-kitty`) | **kitty** | miniature inline ad alta risoluzione + modal hi-res (cap 1600 px sul lato lungo); scroll fluido senza ritrasmissioni; immagini mai sopra picker/modal |
| Altri terminali (Ghostty, iTerm2, Windows Terminal, xterm…) | `catimg` | esperienza **invariata** rispetto a prima della feature: placeholder + modal `catimg` |
| tmux / GNU screen (anche se il terminale è kitty) | `catimg` | il passthrough delle immagini non è affidabile → sempre fallback |
| Pipe / CI / shell headless (non-tty) | `catimg` | nessuna query al terminale → suite e CI restano sicure |
| Terminale non supportato e `catimg` assente | `off` | solo placeholder; il click mostra lo stato «rendering disabilitato», nessun modal |

### Configurazione

La modalità è automatica (`auto`). Per forzarla usa la variabile d'ambiente `IMAGE_PROTOCOL` oppure la chiave `image_protocol` in `config.json`:

```bash
export IMAGE_PROTOCOL=auto    # auto | kitty | catimg | off
```

```json
{
    "image_protocol": "auto"
}
```

I limiti delle miniature sono configurabili allo stesso modo (env o `config.json`):

| Chiave `config.json` | Variabile d'ambiente | Default | Significato |
|---|---|---|---|
| `thumbnail_max_lines` | `THUMBNAIL_MAX_LINES` | `12` | altezza massima della miniatura, in righe |
| `thumbnail_max_cols` | `THUMBNAIL_MAX_COLS` | `60` | larghezza massima, in colonne (comunque clampata alla larghezza della chat) |

> **Nota sviluppo:** il client si lancia con l'alias **`signal`** (log DEBUG su `/tmp/signal-tui.log`).  La validazione manuale su kitty reale segue la checklist **locale** `docs/CHECKLIST_MANUAL_KITTY.md` (gitignored, non distribuita con il repo).  I dettagli tecnici (protocollo, clipping, gestione screen-stack) sono nel design `documentation/design/DESIGN_NATIVE_IMAGES.md` del repo principale (`signal-tui-client`).

## Performance Profiling

Per profilare l'applicazione in termini di **CPU**, **RAM** e **I/O**, usa gli strumenti nella cartella `profiling/`:

```bash
# Monitoraggio risorse (CPU, RAM, I/O nel tempo)
python profiling/monitor_resources.py --duration 120
python profiling/analyze_resources.py

# Flamegraph CPU (py-spy — campiona tutti i thread)
./profiling/run_pyspy.sh 120

# Profiling RAM (tracemalloc)
python profiling/profile_memory.py --duration 120

# Profiling I/O (strace)
./profiling/run_strace.sh 120
```

Vedi [profiling/README.md](profiling/README.md) per istruzioni dettagliate, interpretazione dei risultati e i punti critici noti.

## Testing

### Suite standard (unit + integration, CI-safe)

Esegue tutti i test di `tests/` e `Telegram/` (niente rete, niente account reali):

```bash
make test PYTHON=.venv-test/bin/python      # o: source .venv-test/bin/activate && make test
make lint                                   # ruff check
make coverage                               # test + coverage (gate 68%)
```

Questa suite gira anche in CI (`.github/workflows/ci.yml`, Python 3.12/3.13).

### Test di integrazione "live" (run opzionale, su filo reale)

I test in `tests/test_live_quote_media.py` (E1–E7) verificano il comportamento
end-to-end **contro un account di test reale** ("Roberto BMW", presente sui 3
protocolli) e **inviano messaggi reali**. Non girano in CI: sono sempre skippati
senza `LIVE_TESTS=1`.

**Prerequisiti:**
- Daemon signal-cli attivo (`config.json` con `user_number`) — richiesto per Signal;
- WAHA raggiungibile con sessione WORKING (per WhatsApp);
- `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` + sessione Telethon autorizzata (per Telegram);
- contatto di test "Roberto BMW" presente sui 3 protocolli, oppure override esplicito
  degli id con `LIVE_TARGET_SIGNAL` / `LIVE_TARGET_WHATSAPP` / `LIVE_TARGET_TELEGRAM`.

**Esecuzione:**

```bash
make live-test PYTHON=.venv-test/bin/python
# equivale a: LIVE_TESTS=1 .venv-test/bin/python -m pytest tests/test_live_quote_media.py -v
```

Cosa copre (criteri §10.2 di `docs/DESIGN_QUOTE_MEDIA_37_V2.md`):
- **E1/E2/E3/E7** — Signal: quote media senza/con caption (params `quoteMessage`/`quoteAttachments`
  verificati sul filo), reply a testo invariata, retry dopo fallimento;
- **E5** — WhatsApp: quote foto (`reply_to` = id Baileys);
- **E6** — Telegram: quote foto (`reply_to` = id numerico).

Se nella chat non ci sono media freschi con `content_type` persistito (i media
legacy pre-piano-B non sono backfillati automaticamente), i test Signal stampano
"⏳ INVIA ORA una NUOVA immagine…" e attendono fino a 90 s: basta mandare una foto
dal client ufficiale del contatto di test e il test procede da solo.

**E4 (ingresso, manuale):** richiede di quotare un'immagine dal client ufficiale
del contatto di test mentre il test è in attesa:

```bash
make live-test-manual PYTHON=.venv-test/bin/python
```

**Note:** ogni test invia messaggi marcati `[live-test #37]` (riconoscibili sul
device). I test saltano con messaggi chiari quando un backend non è configurato,
il contatto non è risolto o non c'è un media adatto — non falliscono la suite.

## Project Structure

```
signal-tui-client/
├── signal_tui.py            # Main TUI application (Textual App) — multi-protocol
├── backend/                 # Shared backend: SQLite persistence, signal-cli RPC/subprocess, webhook/HTTP server, receipts
├── backends/                # Per-protocol backend implementations
│   ├── __init__.py          #   Package init — exports ChatBackend, BackendManager, SignalBackend, WhatsAppBackend, TelegramBackend
│   ├── base.py              #   Abstract ChatBackend interface
│   ├── manager.py           #   Multi-backend registry and routing
│   ├── signal.py            #   Signal backend (signal-cli daemon / subprocess, envelope parsing)
│   ├── whatsapp.py          #   WhatsApp backend (WAHA REST + webhook push)
│   ├── telegram.py          #   Telegram backend (Telethon MTProto, QR login, read receipts)
│   └── config.py            #   WhatsApp + Telegram configuration helpers
├── tui/                     # TUI screens, mixins and widgets (Textual App composition)
│   └── images/              # Native kitty rendering: detect.py (terminal detection), cellsize.py, kitty_renderer.py
├── models.py                # Shared data models (ChatContact, ChatMessage, ChatEvent)
├── ui_components.py         # Custom Textual widgets (MessageWidget, ImageWidget, ImageModalScreen, …)
├── emoji_picker.py          # Emoji picker modal screen and auto-completion widget (Ctrl+E)
├── emoji_data.py            # Emoji database (categories, aliases, search index)
├── contact_picker.py        # Contact search picker modal screen (Ctrl+S)
├── device_link_screen.py    # Device link picker (Signal / WhatsApp / Telegram QR pairing)
├── qr_utils.py              # QR code renderer (ASCII / PNG-to-ASCII)
├── link_account.py          # Signal device linking script (QR code — or use Ctrl+L in TUI)
├── link_whatsapp.py         # WhatsApp device linking script (QR code — or use Ctrl+L in TUI)
├── migrate_cache_sqlite.py  # One-shot migration: JSON cache → SQLite
├── migrate_cache_protocol.py# One-shot migration: add protocol field to cache
├── migrate_cache_status.py  # One-shot migration: add status field to cache
├── purge_whatsapp_cache.py  # Utility: purge WhatsApp messages from cache
├── Telegram/                # Telegram test suite (74 tests)
│   ├── test_telegram_backend.py  (35 tests)
│   └── test_regression.py        (39 tests)
├── tests/                   # Test suite (pytest, 1006 tests)
│   ├── conftest.py
│   ├── test_whatsapp_backend.py (102 tests)
│   ├── test_ui_protocol.py      (55 tests)
│   ├── test_typing_indicator.py (29 tests)
│   ├── test_backend_lazy_config.py (5 tests)
│   └── ... (24 test files total)
│   └── run_regression_tests.sh  # legacy (superseded by Makefile)
├── profiling/               # Performance profiling tools (CPU, RAM, I/O)
├── scripts/                 # Helper scripts (start_whatsapp.sh)
├── docs/                    # Documentation & design notes
│   ├── BUGS.md              #   Known bugs and limitations
│   ├── TEST_REPORT.md       #   Test report (last run: 1080/1080 ✅)
│   ├── PERF_ANALYSIS.md     #   Performance analysis (UI reactivity hotspots)
│   ├── DESIGN_*.md          #   Design documents (edit messages, ctrls rubrica, UI freeze, fixes)
│   └── PLAN_*.md            #   Work/implementation plans (testing, UI block analysis, …)
├── assets/screenshots/      # README screenshots (main UI, image viewer)
├── docker-compose.yml       # WAHA (WhatsApp HTTP API) Docker container
├── .env.example             # Template for WAHA + Telegram credentials
├── .dockerignore            # Docker build exclusions
├── install.sh               # Automatic installation script
├── Makefile                 # Shared commands: make test / lint / coverage / check
├── pyproject.toml           # Config condivisa pytest / coverage / ruff
├── .github/workflows/ci.yml # CI: lint + test (matrice 3.12/3.13) + coverage gate + Codecov
├── requirements.txt         # Python dependencies (textual, telethon, qrcode, ...)
├── requirements-dev.txt     # Development dependencies (pytest, pytest-cov, coverage, ruff)
├── config.json              # Local configuration (not committed)
├── README.md                # This file
├── bin/                     # signal-cli binaries (not committed)
└── LICENSE                  # GPLv3
```


## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
