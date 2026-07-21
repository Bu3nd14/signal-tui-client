# Signal TUI Client

A terminal-based (TUI) Signal client built with [Textual](https://textual.textualize.io/).

Uses `signal-cli` daemon via JSON-RPC over HTTP for fast operations, with automatic fallback to subprocess if the daemon is unavailable.

![Main interface](screenshot.png)
*Main chat interface*

![Image modal viewer](screenshot2.png)
*Fullscreen image viewer modal*

![Image modal viewer (alternate)](screenshot3.png)
*Fullscreen image viewer modal (alternate view)*

## Features

- 📱 Full contact list with unread badges
- 💬 Real-time message receiving and sending
- 🖼️ Native terminal image rendering (via `catimg`) with fullscreen modal viewer
- 📜 Message history with local cache (last 200 messages per contact, 3-day retention)
- 🔗 Device linking via QR code
- ⚡ Daemon mode for fast JSON-RPC communication
- 🔄 Automatic fallback to subprocess if daemon is not running

## Prerequisites

- **Python 3.10+**
- **signal-cli** — download and place in `./bin/` directory (see Installation)
- **catimg** — for rendering images in the terminal (optional; falls back to text placeholder if missing)
- A linked Signal account (see Device Linking)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Bu3nd14/signal-tui-client.git
cd signal-tui-client
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Download signal-cli

Download the latest `signal-cli` release for your platform:

```bash
# Example for Linux x86_64
mkdir -p bin
cd bin
wget https://github.com/AsamK/signal-cli/releases/latest/download/signal-cli-X.Y.Z-Linux.tar.gz
tar xzf signal-cli-X.Y.Z-Linux.tar.gz
rm signal-cli-X.Y.Z-Linux.tar.gz
cd ..
```

The app will automatically find `signal-cli` in the `./bin/signal-cli-*/` directory.

### 4. Install catimg (optional, for image rendering)

```bash
sudo apt install catimg
```

### 5. Configure your phone number

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

## Device Linking

Before using the client, you need to link your Signal account:

```bash
python3 link_account.py
```

This will display a QR code. Scan it with the Signal app on your phone (Settings → Linked Devices → Link New Device).

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
| Type message + `Enter` | Send message |
| `Ctrl+Q` | Quit |
| `Ctrl+C` | Quit |

### Tips

- The app starts the `signal-cli` daemon automatically on first launch
- Messages are cached locally in `~/.local/share/signal-tui-client/messages.json`
- Only the last 20 messages are shown when opening a chat; click "Load more" to see all cached messages
- Unread messages are shown with a `*N` badge next to the contact name

## Project Structure

```
signal-tui-client/
├── signal_tui.py          # Main TUI application (Textual App)
├── backend.py             # Backend: signal-cli communication, cache, data models
├── ui_components.py       # Custom Textual widgets
├── link_account.py        # Device linking script (QR code)
├── requirements.txt       # Python dependencies
├── config.json            # Local configuration (not committed)
├── bin/                   # signal-cli binaries (not committed)
└── LICENSE                # GPLv3
```

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
