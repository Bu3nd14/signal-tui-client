"""
Signal TUI Client — Interfaccia Textual integrata con signal-cli via JSON-RPC.
Usa signal-cli daemon su HTTP (localhost) per operazioni veloci (millisecondi).
Se il daemon non è disponibile, ricade su subprocess (più lento ma funziona).
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

    def _add_message(self, text: str, is_mine: bool = False, is_info: bool = False):
        """Aggiunge un messaggio alla chat con allineamento corretto."""
        chat_log = self.query_one("#chat-log", Vertical)
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

            # Carica messaggi esistenti
            self.run_worker(
                self._load_messages_worker, exclusive=False, thread=True
            )

            # Avvia polling in un thread worker (non blocca la UI)
            self._polling_active = True
            self.run_worker(
                self._poll_worker, exclusive=False, thread=True
            )

    # ─── Logica messaggi ────────────────────────────────────────────────────

    def _is_message_for_contact(self, envelope: dict, contact: Contact) -> bool:
        """Verifica se un envelope riguarda il contatto selezionato."""
        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")

        # Messaggio diretto dal contatto
        if contact.number in source or contact.number in source_number or contact.aci in source_uuid:
            return True

        # SyncMessage (messaggio inviato da noi al contatto)
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            dest = sent.get("destination", "")
            dest_number = sent.get("destinationNumber", "")
            dest_uuid = sent.get("destinationUuid", "")
            if contact.number in dest or contact.number in dest_number or contact.aci in dest_uuid:
                return True

        return False

    def _extract_message_text(self, envelope: dict) -> tuple[str, str, bool] | None:
        """Estrae (sender_label, text, is_mine) da un envelope.
        is_mine=True per messaggi inviati da noi (sync), False per messaggi ricevuti."""
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        # dataMessage — messaggio ricevuto
        data_msg = envelope.get("dataMessage", {})
        if data_msg:
            text = data_msg.get("message", "")
            if text:
                sender = source_name or source_number
                return (sender, text, False)

        # syncMessage.sentMessage — messaggio inviato da altro dispositivo
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "")
            if text:
                return ("Tu", text, True)

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

    def _load_messages_worker(self):
        """Carica i messaggi recenti in un thread worker."""
        if not self.selected_contact:
            return

        self.call_from_thread(self._add_message, "⏳ Caricamento messaggi...", is_info=True)

        if self._use_daemon and self.rpc:
            messages = self.rpc.receive()
            contact = self.selected_contact
            found = 0

            for msg in messages:
                envelope = msg.get("envelope", {})
                if not self._is_message_for_contact(envelope, contact):
                    continue

                ts = self._get_message_timestamp(envelope)
                if ts:
                    self._seen_timestamps.add(ts)

                result = self._extract_message_text(envelope)
                if result:
                    found += 1
                    sender, text, is_mine = result
                    if is_mine:
                        line = f"Tu: {text}"
                    else:
                        line = f"{sender}: {text}"
                    self.call_from_thread(self._add_message, line, is_mine=is_mine)

            if found == 0:
                self.call_from_thread(
                    self._add_message,
                    "Nessun messaggio recente per questo contatto",
                    is_info=True,
                )
        else:
            try:
                output = _run_subprocess(["receive"])
                for line in output.strip().split("\n"):
                    if self.selected_contact.number in line:
                        self.call_from_thread(
                            self._add_message, line[:200], is_info=True
                        )
            except Exception as e:
                self.call_from_thread(
                    self._add_message,
                    f"⚠️ Errore ricezione: {e}",
                    is_info=True,
                )

        self.call_from_thread(self._add_message, "✅ Messaggi caricati", is_info=True)

    def _poll_worker(self):
        """Thread worker che fa polling ogni 1 secondo (non blocca la UI)."""
        while self._polling_active and self.selected_contact and self._use_daemon and self.rpc:
            try:
                messages = self.rpc.receive()
                contact = self.selected_contact

                for msg in messages:
                    envelope = msg.get("envelope", {})
                    if not self._is_message_for_contact(envelope, contact):
                        continue

                    ts = self._get_message_timestamp(envelope)
                    if ts and ts not in self._seen_timestamps:
                        self._seen_timestamps.add(ts)

                        result = self._extract_message_text(envelope)
                        if result:
                            sender, text, is_mine = result
                            if is_mine:
                                line = f"Tu: {text}"
                            else:
                                line = f"{sender}: {text}"
                            self.call_from_thread(
                                self._add_message, line, is_mine=is_mine
                            )
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
        self._add_message(f"Tu: {message}", is_mine=True)
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
