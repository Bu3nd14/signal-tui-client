"""
Backend per Signal TUI Client.
Gestisce la comunicazione con signal-cli (JSON-RPC via HTTP o subprocess),
la cache dei messaggi su disco, e il modello dati Contact.
Nessuna dipendenza da Textual.
"""

import json
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# ─── Costanti ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
USER_NUMBER = "+393482581393"
DAEMON_HTTP_PORT = 8080
DAEMON_URL = f"http://127.0.0.1:{DAEMON_HTTP_PORT}/api/v1/rpc"
CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
CACHE_RETENTION_DAYS = 3


# ─── Signal CLI ──────────────────────────────────────────────────────────────

def _find_signal_cli() -> Path:
    """Cerca l'eseguibile signal-cli nella directory ./bin/ del progetto."""
    bin_dir = PROJECT_DIR / "bin"
    for d in bin_dir.iterdir():
        if d.is_dir() and d.name.startswith("signal-cli-"):
            exe = d / "bin" / "signal-cli"
            if exe.exists() and exe.stat().st_mode & 0o111:
                return exe
    raise FileNotFoundError("signal-cli non trovato in ./bin/")


SIGNAL_CLI_PATH = _find_signal_cli()


def find_signal_cli() -> Path:
    """Funzione di utilità pubblica per trovare signal-cli."""
    return _find_signal_cli()


def _is_daemon_running() -> bool:
    """Verifica se il daemon signal-cli è già in esecuzione."""
    try:
        rpc = SignalRPCClient()
        test = rpc._call("listContacts")
        return "result" in test
    except Exception:
        return False


def _run_subprocess(args: list[str]) -> str:
    """Esegue signal-cli via subprocess e restituisce stdout."""
    result = subprocess.run(
        [str(SIGNAL_CLI_PATH), "-u", USER_NUMBER] + args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"signal-cli error (code {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


# ─── Cache messaggi ──────────────────────────────────────────────────────────

def _ensure_cache_dir():
    """Crea la directory della cache se non esiste."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_cache() -> dict[str, list[dict]]:
    """Carica tutti i messaggi dalla cache."""
    _ensure_cache_dir()
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict[str, list[dict]]):
    """Salva tutti i messaggi nella cache."""
    _ensure_cache_dir()
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get_cached_messages(contact_number: str) -> list[dict]:
    """Restituisce i messaggi in cache per un contatto."""
    cache = _load_cache()
    return cache.get(contact_number, [])


def _add_message_to_cache(
    contact_number: str,
    text: str,
    is_mine: bool,
    sender: str,
    timestamp: int,
    quote_text: str | None = None,
    msg_type: str = "text",
    attachment_info: str | None = None,
):
    """Aggiunge un messaggio alla cache.
    msg_type: "text", "image", "sticker", "attachment"
    attachment_info: dettagli aggiuntivi (nome file, emoji sticker, ecc.)
    """
    cache = _load_cache()
    if contact_number not in cache:
        cache[contact_number] = []
    cache[contact_number].append({
        "text": text,
        "is_mine": is_mine,
        "sender": sender,
        "timestamp": timestamp,
        "quote_text": quote_text,
        "msg_type": msg_type,
        "attachment_info": attachment_info,
        "read": is_mine,  # i nostri messaggi sono già letti
    })
    _save_cache(cache)
    _prune_cache()


def _prune_cache():
    """Rimuove i messaggi più vecchi di CACHE_RETENTION_DAYS giorni
    e limita a 200 messaggi per contatto."""
    cache = _load_cache()
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
    modified = False

    for contact in list(cache.keys()):
        # Rimuovi messaggi vecchi
        before = len(cache[contact])
        cache[contact] = [m for m in cache[contact] if m.get("timestamp", 0) >= cutoff]
        after = len(cache[contact])
        if before != after:
            modified = True

        # Limita a 200 messaggi per contatto
        if len(cache[contact]) > 200:
            cache[contact] = cache[contact][-200:]
            modified = True

        if not cache[contact]:
            del cache[contact]
            modified = True

    if modified:
        _save_cache(cache)


def _mark_as_read(contact_number: str):
    """Segna tutti i messaggi di un contatto come letti."""
    cache = _load_cache()
    if contact_number in cache:
        modified = False
        for msg in cache[contact_number]:
            if not msg.get("read", True):
                msg["read"] = True
                modified = True
        if modified:
            _save_cache(cache)


def _count_unread() -> dict[str, int]:
    """Conta i messaggi non letti per ogni contatto.
    Messaggi senza campo 'read' (vecchia cache) sono considerati letti."""
    cache = _load_cache()
    counts = {}
    for number, messages in cache.items():
        unread = sum(
            1 for m in messages
            if not m.get("is_mine") and not m.get("read", True)
        )
        if unread > 0:
            counts[number] = unread
    return counts


# ─── JSON-RPC Client via HTTP ────────────────────────────────────────────────

class SignalRPCClient:
    """Client JSON-RPC per comunicare con signal-cli daemon via HTTP."""

    def __init__(self, url: str = DAEMON_URL):
        self.url = url
        self._req_id = 0

    def _call(self, method: str, params: dict | None = None) -> dict:
        """Esegue una chiamata JSON-RPC e restituisce il risultato."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                response_data = resp.read().decode("utf-8")
                return json.loads(response_data)
        except Exception as e:
            return {"error": str(e)}

    def list_contacts(self) -> list[dict]:
        """Recupera la lista contatti."""
        result = self._call("listContacts")
        if "error" in result:
            return []
        return result.get("result", [])

    def send_message(self, message: str, recipient: str) -> dict:
        """Invia un messaggio a un destinatario."""
        params = {
            "message": message,
            "recipient": [recipient],
        }
        return self._call("send", params)

    def receive(self) -> list[dict]:
        """Riceve i messaggi."""
        result = self._call("receive")
        if "error" in result:
            return []
        return result.get("result", [])


# ─── Modello dati ────────────────────────────────────────────────────────────

class Contact:
    """Rappresenta un contatto Signal."""

    def __init__(self, number: str, name: str = "", aci: str = ""):
        self.number = number
        self.name = name if name else number
        self.aci = aci

    @property
    def display_name(self) -> str:
        return self.name if self.name else self.number
