"""
Signal TUI Client — Interfaccia Textual integrata con signal-cli via JSON-RPC.
Usa signal-cli daemon su HTTP (localhost) per operazioni veloci (millisecondi).
Se il daemon non è disponibile, ricade su subprocess (più lento ma funziona).
I messaggi vengono salvati in cache locale per persistenza tra sessioni.
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header,
    Footer,
    ListView,
    ListItem,
    Label,
    Input,
    Static,
)


# ─── Costanti ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
USER_NUMBER = "+393482581393"
DAEMON_HTTP_PORT = 8080
DAEMON_URL = f"http://127.0.0.1:{DAEMON_HTTP_PORT}/api/v1/rpc"
CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"
CACHE_RETENTION_DAYS = 10


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
):
    """Aggiunge un messaggio alla cache."""
    cache = _load_cache()
    if contact_number not in cache:
        cache[contact_number] = []
    cache[contact_number].append({
        "text": text,
        "is_mine": is_mine,
        "sender": sender,
        "timestamp": timestamp,
        "quote_text": quote_text,
    })
    _save_cache(cache)
    _prune_cache()


def _prune_cache():
    """Rimuove i messaggi più vecchi di CACHE_RETENTION_DAYS giorni."""
    cache = _load_cache()
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
    modified = False

    for contact in list(cache.keys()):
        before = len(cache[contact])
        cache[contact] = [m for m in cache[contact] if m.get("timestamp", 0) >= cutoff]
        after = len(cache[contact])
        if before != after:
            modified = True
        if not cache[contact]:
            del cache[contact]
            modified = True

    if modified:
        _save_cache(cache)


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


# ─── Widgets ─────────────────────────────────────────────────────────────────

class ContactListWidget(Vertical):
    """Colonna sinistra: lista contatti."""

    def compose(self):
        yield Label("📇 Contatti", classes="section-title")
        yield ListView(id="contact-list")

    def on_mount(self):
        self.styles.width = 30


class ChatAreaWidget(Vertical):
    """Colonna destra: area messaggi + input."""

    def compose(self):
        yield Label("💬 Chat", classes="section-title")
        yield Vertical(id="chat-log")
        yield Input(placeholder="Scrivi un messaggio...", id="message-input")


# ─── App principale ──────────────────────────────────────────────────────────

class SignalTUI(App):
    """App principale Signal TUI con daemon JSON-RPC via HTTP."""

    CSS = """
    Screen {
        background: $surface;
    }

    .section-title {
        text-style: bold;
        padding: 1 1;
        background: $accent;
        color: $text;
        width: 100%;
    }

    #contact-list {
        height: 100%;
        border: solid $border;
    }

    #contact-list ListItem {
        padding: 1 1;
    }

    #contact-list ListItem:hover {
        background: $accent 20%;
    }

    #contact-list ListItem:focus {
        background: $accent 40%;
    }

    #chat-log {
        height: 1fr;
        border: solid $border;
        margin: 0 1;
        overflow-y: auto;
        overflow-x: hidden;
    }

    .msg-left {
        text-align: left;
        padding: 0 1;
        color: $text;
    }

    .msg-right {
        text-align: right;
        padding: 0 1;
        color: $success;
    }

    .msg-info {
        text-align: left;
        padding: 0 1;
        color: $text-muted;
    }

    .msg-quote {
        text-align: left;
        padding: 0 1 0 3;
        color: $text-muted;
        text-style: italic;
    }

    #message-input {
        dock: bottom;
        margin: 1 1;
    }

    Horizontal {
        height: 1fr;
    }
    """

    def __init__(self):
        super().__init__()
        self.contacts: list[Contact] = []
        self.selected_contact: Optional[Contact] = None
        self.daemon_proc: Optional[subprocess.Popen] = None
        self.rpc: Optional[SignalRPCClient] = None
        self._use_daemon = False
        self._polling_active = False
        self._seen_timestamps: set[int] = set()

    def compose(self):
        yield Header()
        yield Horizontal(
            ContactListWidget(),
            ChatAreaWidget(),
        )
        yield Footer()

    def on_mount(self):
        """All'avvio, avvia il daemon e carica i contatti."""
        self.run_worker(self._startup, exclusive=True, thread=True)

    def action_quit(self):
        """Ctrl+Q: ferma il polling ed esce pulitamente."""
        self._polling_active = False
        self.exit()

    def on_exit(self):
        """Alla chiusura, ferma il polling e NON killiamo il daemon."""
        self._polling_active = False

    # ─── Metodi helper per la chat ──────────────────────────────────────────

    def _add_message(
        self,
        text: str,
        is_mine: bool = False,
        is_info: bool = False,
        quote_text: str | None = None,
    ):
        """Aggiunge un messaggio alla chat con allineamento corretto.
        Se quote_text è presente, mostra prima la citazione in stile quote."""
        chat_log = self.query_one("#chat-log", Vertical)

        # Se c'è una citazione, mostrala prima
        if quote_text:
            quote_widget = Static(f"▎ {quote_text}", classes="msg-quote")
            chat_log.mount(quote_widget)

        if is_info:
            widget = Static(text, classes="msg-info")
        elif is_mine:
            widget = Static(text, classes="msg-right")
        else:
            widget = Static(text, classes="msg-left")
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    def _clear_chat(self):
        """Pulisce la chat."""
        chat_log = self.query_one("#chat-log", Vertical)
        chat_log.remove_children()

    # ─── Identificazione contatto per envelope ──────────────────────────────

    def _identify_contact_for_envelope(self, envelope: dict) -> Optional[Contact]:
        """Identifica a quale contatto (tra quelli in rubrica) appartiene un envelope.
        Confronto esatto con ==, nessun substring match.
        
        Per i syncMessage (inviati da noi), matcha sul destinatario (destination).
        Per i messaggi ricevuti, matcha sul mittente (source)."""
        # SyncMessage (messaggio inviato da noi a un contatto) — PRIORITÀ!
        # Il source di un syncMessage è il NOSTRO numero, non il contatto.
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            dest = sent.get("destination", "")
            dest_number = sent.get("destinationNumber", "")
            dest_uuid = sent.get("destinationUuid", "")
            for contact in self.contacts:
                if dest == contact.number or dest_number == contact.number:
                    return contact
                if dest_uuid and contact.aci and dest_uuid == contact.aci:
                    return contact

        # Messaggio diretto dal contatto
        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")
        for contact in self.contacts:
            if source == contact.number or source_number == contact.number:
                return contact
            if source_uuid and contact.aci and source_uuid == contact.aci:
                return contact

        return None

    def _extract_message_text(self, envelope: dict) -> tuple[str, str, bool, str | None] | None:
        """Estrae (sender_label, text, is_mine, quote_text) da un envelope.
        is_mine=True per messaggi inviati da noi (sync), False per messaggi ricevuti.
        quote_text è il testo del messaggio citato, se presente."""
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        # dataMessage — messaggio ricevuto
        data_msg = envelope.get("dataMessage", {})
        if data_msg:
            text = data_msg.get("message", "")
            if text:
                sender = source_name or source_number
                # Estrai citazione (quote)
                quote = data_msg.get("quote", {})
                quote_text = quote.get("text", "") if quote else None
                return (sender, text, False, quote_text)

        # syncMessage.sentMessage — messaggio inviato da altro dispositivo
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "")
            if text:
                # Estrai citazione anche dai syncMessage
                quote = sent.get("quote", {})
                quote_text = quote.get("text", "") if quote else None
                return ("Tu", text, True, quote_text)

        return None

    def _get_message_timestamp(self, envelope: dict) -> int:
        """Restituisce il timestamp del messaggio."""
        ts = envelope.get("timestamp", 0)
        if not ts:
            data = envelope.get("dataMessage", {})
            ts = data.get("timestamp", 0)
        if not ts:
            sync = envelope.get("syncMessage", {})
            sent = sync.get("sentMessage", {})
            ts = sent.get("timestamp", 0)
        return ts

    # ─── Processamento envelope (salva in cache + mostra se contatto corrente) ─

    def _process_envelope(self, envelope: dict) -> bool:
        """Processa un envelope: identifica il contatto, salva in cache,
        e se è il contatto corrente, mostra nella UI.
        Restituisce True se il messaggio è stato mostrato."""
        contact = self._identify_contact_for_envelope(envelope)
        if contact is None:
            return False

        ts = self._get_message_timestamp(envelope)
        result = self._extract_message_text(envelope)
        if result is None:
            return False

        sender, text, is_mine, quote_text = result

        # Salva in cache (sempre, per qualsiasi contatto)
        _add_message_to_cache(
            contact_number=contact.number,
            text=text,
            is_mine=is_mine,
            sender=sender,
            timestamp=ts,
            quote_text=quote_text,
        )

        # Mostra nella UI solo se è il contatto corrente
        if self.selected_contact and contact.number == self.selected_contact.number:
            if ts:
                self._seen_timestamps.add(ts)
            self.call_from_thread(
                self._add_message, text, is_mine=is_mine, quote_text=quote_text
            )
            return True

        return False

    # ─── Startup ────────────────────────────────────────────────────────────

    def _startup(self):
        """Avvia signal-cli daemon e carica i contatti."""
        self.call_from_thread(self._add_message, "⏳ Avvio signal-cli daemon...", is_info=True)
        self.rpc = SignalRPCClient()

        # Verifica se il daemon è già in esecuzione
        if _is_daemon_running():
            self._use_daemon = True
            self.call_from_thread(
                self._add_message, "✅ Daemon già attivo, collegamento diretto...", is_info=True
            )
            self._load_contacts_rpc()
            return

        # Altrimenti avvia il daemon
        self.call_from_thread(
            self._add_message, "⏳ Avvio signal-cli daemon...", is_info=True
        )

        self.daemon_proc = subprocess.Popen(
            [
                str(SIGNAL_CLI_PATH),
                "-u", USER_NUMBER,
                "daemon",
                "--http", f"127.0.0.1:{DAEMON_HTTP_PORT}",
                "--receive-mode", "on-connection",
                "--no-receive-stdout",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Aspetta che il server HTTP sia pronto
        for _ in range(15):
            try:
                test = self.rpc._call("listContacts")
                if "result" in test:
                    self._use_daemon = True
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            self.call_from_thread(
                self._add_message,
                "❌ Daemon non disponibile. Uso modalità subprocess (più lenta).",
                is_info=True,
            )
            self._use_daemon = False
            self._load_contacts_subprocess()
            return

        self._load_contacts_rpc()

    def _load_contacts_rpc(self):
        """Carica i contatti via JSON-RPC (daemon già attivo)."""
        self.call_from_thread(
            self._add_message, "⏳ Caricamento contatti...", is_info=True
        )

        contacts_data = self.rpc.list_contacts()
        if isinstance(contacts_data, list) and len(contacts_data) > 0:
            self._parse_and_update_contacts(contacts_data)
        else:
            self.call_from_thread(
                self._add_message,
                "⚠️ RPC non ha restituito contatti. Provo con subprocess...",
                is_info=True,
            )
            self._load_contacts_subprocess()

    def _load_contacts_subprocess(self):
        """Carica i contatti via subprocess (fallback)."""
        self.call_from_thread(
            self._add_message, "⏳ Caricamento contatti (subprocess)...", is_info=True
        )

        try:
            output = _run_subprocess(["listContacts"])
            contacts = self._parse_contacts_from_output(output)
            self.contacts = contacts
            self.call_from_thread(self._update_contacts_ui, contacts)
        except Exception as e:
            self.call_from_thread(
                self._add_message,
                f"❌ Errore caricamento contatti: {e}",
                is_info=True,
            )

    def _parse_contacts_from_output(self, output: str) -> list[Contact]:
        """Parsa l'output di 'signal-cli listContacts'."""
        contacts = []
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split()
            number = ""
            name = ""
            aci = ""
            for i, p in enumerate(parts):
                if p.startswith("Number:"):
                    number = p.split(":", 1)[1].strip()
                elif p.startswith("Name:"):
                    name = p.split(":", 1)[1].strip()
                elif p.startswith("ACI:"):
                    aci_val = p.split(":", 1)[1].strip()
                    if aci_val and aci_val != "-":
                        aci = aci_val
                elif p == "Profile" and i + 1 < len(parts) and parts[i + 1].startswith("name:"):
                    profile_name = parts[i + 1].split(":", 1)[1].strip()
                    if profile_name and not name:
                        name = profile_name
            if number:
                contacts.append(Contact(number=number, name=name, aci=aci))
        return contacts

    def _parse_and_update_contacts(self, contacts_data: list[dict]):
        """Parsa i dati dei contatti e aggiorna l'interfaccia."""
        contacts = []
        for c in contacts_data:
            number = c.get("number", "")
            # Il nome può essere in name, givenName, o profile.givenName
            name = (
                c.get("name")
                or c.get("givenName")
                or (c.get("profile") or {}).get("givenName")
                or number
            )
            aci = c.get("uuid", "") or c.get("aci", "")
            contacts.append(Contact(number=number, name=name, aci=aci))

        self.contacts = contacts
        self.call_from_thread(self._update_contacts_ui, contacts)

    def _update_contacts_ui(self, contacts: list[Contact]):
        """Aggiorna l'interfaccia con la lista contatti."""
        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        for c in contacts:
            contact_list.append(ListItem(Label(f"📱 {c.display_name}")))

        self._add_message(f"✅ Caricati {len(contacts)} contatti.", is_info=True)
        self._add_message("💡 Seleziona un contatto per vedere la chat", is_info=True)

    # ─── Selezione contatto ─────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected):
        """Quando un contatto viene selezionato, mostra la chat e avvia polling."""
        index = self.query_one("#contact-list", ListView).index
        if index is not None and 0 <= index < len(self.contacts):
            self.selected_contact = self.contacts[index]
            self._seen_timestamps.clear()
            self._clear_chat()
            self._add_message(
                f"📱 Chat con: {self.selected_contact.display_name}", is_info=True
            )
            self._add_message(self.selected_contact.number, is_info=True)
            self._add_message("─" * 40, is_info=True)

            # Ferma polling precedente se attivo
            self._polling_active = False

            # Carica messaggi dalla cache + nuovi
            self.run_worker(
                self._load_messages_worker, exclusive=False, thread=True
            )

            # Avvia polling in un thread worker (non blocca la UI)
            self._polling_active = True
            self.run_worker(
                self._poll_worker, exclusive=False, thread=True
            )

    # ─── Logica messaggi ────────────────────────────────────────────────────

    def _load_messages_worker(self):
        """Carica i messaggi: prima dalla cache, poi receive() per nuovi."""
        if not self.selected_contact:
            return

        contact = self.selected_contact

        # 1. Carica messaggi dalla cache
        cached = _get_cached_messages(contact.number)
        if cached:
            for msg in cached:
                text = msg.get("text", "")
                is_mine = msg.get("is_mine", False)
                sender = msg.get("sender", "")
                quote_text = msg.get("quote_text")
                ts = msg.get("timestamp", 0)

                if ts:
                    self._seen_timestamps.add(ts)

                self.call_from_thread(
                    self._add_message, text, is_mine=is_mine, quote_text=quote_text
                )

            self.call_from_thread(
                self._add_message,
                f"📋 Caricati {len(cached)} messaggi dalla cronologia",
                is_info=True,
            )
        else:
            self.call_from_thread(
                self._add_message, "Nessun messaggio in cronologia per questo contatto", is_info=True
            )

        # 2. Receive per eventuali nuovi messaggi
        if self._use_daemon and self.rpc:
            messages = self.rpc.receive()
            nuovi = 0
            for msg in messages:
                envelope = msg.get("envelope", {})
                if self._process_envelope(envelope):
                    nuovi += 1

            if nuovi > 0:
                self.call_from_thread(
                    self._add_message, f"📨 {nuovi} nuovi messaggi", is_info=True
                )

        self.call_from_thread(self._add_message, "✅ Pronto", is_info=True)

    def _poll_worker(self):
        """Thread worker che fa polling ogni 1 secondo (non blocca la UI).
        Processa TUTTI i messaggi: li salva in cache e mostra solo quelli
        per il contatto corrente."""
        while self._polling_active and self.selected_contact and self._use_daemon and self.rpc:
            try:
                messages = self.rpc.receive()
                for msg in messages:
                    envelope = msg.get("envelope", {})
                    self._process_envelope(envelope)
            except Exception:
                pass

            # Aspetta 1 secondo prima del prossimo poll
            for _ in range(10):
                if not self._polling_active:
                    return
                time.sleep(0.1)

    # ─── Invio messaggi ─────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted):
        """Invia un messaggio quando l'utente preme Invio."""
        if not self.selected_contact:
            self._add_message("❌ Seleziona prima un contatto!", is_info=True)
            return

        message = event.value.strip()
        if not message:
            return

        # Mostra subito il messaggio nella UI (allineato a destra)
        self._add_message(message, is_mine=True)

        # Salva in cache
        _add_message_to_cache(
            contact_number=self.selected_contact.number,
            text=message,
            is_mine=True,
            sender="Tu",
            timestamp=int(time.time() * 1000),
        )

        event.input.value = ""

        # Invia in un thread worker
        self.run_worker(
            lambda msg=message: self._send_message_worker(msg),
            exclusive=False,
            thread=True,
        )

    def _send_message_worker(self, message: str):
        """Invia un messaggio (via RPC o subprocess fallback)."""
        if not self.selected_contact:
            return

        if self._use_daemon and self.rpc:
            result = self.rpc.send_message(message, self.selected_contact.number)
            if "error" in result:
                self.call_from_thread(
                    self._add_message,
                    f"❌ Errore invio: {result['error']}",
                    is_info=True,
                )
        else:
            try:
                _run_subprocess([
                    "send",
                    "-m", message,
                    self.selected_contact.number,
                ])
            except Exception as e:
                self.call_from_thread(
                    self._add_message,
                    f"❌ Errore invio: {e}",
                    is_info=True,
                )


def find_signal_cli() -> Path:
    """Funzione di utilità per trovare signal-cli."""
    return _find_signal_cli()


if __name__ == "__main__":
    import signal as signal_module

    app = SignalTUI()

    def _handle_sigint(sig, frame):
        """Gestisce Ctrl+C: ferma il polling ed esce pulitamente."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
