"""
Signal TUI Client — Interfaccia Textual integrata con signal-cli via JSON-RPC.
Usa signal-cli daemon su HTTP (localhost) per operazioni veloci (millisecondi).
Se il daemon non è disponibile, ricade su subprocess (più lento ma funziona).
I messaggi vengono salvati in cache locale per persistenza tra sessioni.
"""

import subprocess
import time
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
    Button,
)

from backend import (
    Contact,
    SignalRPCClient,
    _load_cache,
    _save_cache,
    _prune_cache,
    _is_daemon_running,
    _run_subprocess,
    SIGNAL_CLI_PATH,
    USER_NUMBER,
    DAEMON_HTTP_PORT,
)
from ui_components import ContactListWidget, ChatAreaWidget


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

    .msg-load-more {
        text-align: center;
        padding: 1 1;
        color: $accent;
        text-style: bold;
        background: $surface;
        border: solid $accent;
        margin: 1 0;
    }

    .msg-load-more:hover {
        background: $accent 20%;
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
        self._unread_counts: dict[str, int] = {}
        self._cache: dict[str, list[dict]] = {}
        self._loaded_all = False

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
        msg_type: str = "text",
        attachment_info: str | None = None,
    ):
        """Aggiunge un messaggio alla chat con allineamento corretto."""
        if text is None:
            text = ""

        chat_log = self.query_one("#chat-log", Vertical)

        if quote_text:
            quote_widget = Static(f"▎ {quote_text}", classes="msg-quote")
            chat_log.mount(quote_widget)

        display_text = text
        if msg_type == "image":
            display_text = f"🖼️ {text}" if text and text != "Media" else "🖼️ [Immagine]"
        elif msg_type == "sticker":
            display_text = f"🎨 {text}" if text and text != "Media" else "🎨 [Sticker]"
        elif msg_type == "attachment":
            display_text = f"📎 {text}" if text and text != "Media" else "📎 [File]"

        if is_info:
            widget = Static(display_text, classes="msg-info")
        elif is_mine:
            widget = Static(display_text, classes="msg-right")
        else:
            widget = Static(display_text, classes="msg-left")
        chat_log.mount(widget)
        chat_log.scroll_end(animate=False)

    def _clear_chat(self):
        """Pulisce la chat."""
        chat_log = self.query_one("#chat-log", Vertical)
        chat_log.remove_children()

    # ─── Identificazione contatto per envelope ──────────────────────────────

    def _identify_contact_for_envelope(self, envelope: dict) -> Optional[Contact]:
        """Identifica a quale contatto appartiene un envelope."""
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

        source = envelope.get("source", "")
        source_number = envelope.get("sourceNumber", "")
        source_uuid = envelope.get("sourceUuid", "")
        for contact in self.contacts:
            if source == contact.number or source_number == contact.number:
                return contact
            if source_uuid and contact.aci and source_uuid == contact.aci:
                return contact

        if sent:
            dest = sent.get("destination", "")
            for contact in self.contacts:
                if dest == contact.number:
                    return contact

        return None

    def _extract_message_data(self, envelope: dict) -> dict | None:
        """Estrae i dati di un messaggio da un envelope."""
        source_name = envelope.get("sourceName", "")
        source_number = envelope.get("sourceNumber", "") or envelope.get("source", "")

        def _classify_attachments(attachments: list) -> tuple[str, str]:
            if not attachments:
                return ("text", None)
            for att in attachments:
                content_type = att.get("contentType", "") or ""
                fname = att.get("filename", "") or ""
                caption = att.get("caption", "") or ""
                if content_type.startswith("image/"):
                    info = caption or f"Immagine: {fname}" if fname else "🖼️ Immagine"
                    return ("image", info)
                if content_type.startswith("video/"):
                    info = caption or f"Video: {fname}" if fname else "🎬 Video"
                    return ("attachment", info)
                if content_type.startswith("audio/"):
                    info = caption or f"Audio: {fname}" if fname else "🎵 Audio"
                    return ("attachment", info)
                info = caption or fname or content_type or "📎 File"
                return ("attachment", info)
            return ("attachment", "📎 File")

        def _extract_sticker(sticker: dict | None) -> tuple[str, str] | None:
            if not sticker:
                return None
            pack_id = sticker.get("packId", "")
            sticker_id = sticker.get("stickerId", "")
            info = f"Sticker #{sticker_id}"
            if pack_id:
                info = f"Sticker #{sticker_id} (pack:{pack_id[:8]}…)"
            return ("sticker", info)

        data_msg = envelope.get("dataMessage", {})
        if data_msg:
            text = data_msg.get("message", "") or ""
            sender = source_name or source_number
            quote = data_msg.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(data_msg.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return {
                    "sender": sender, "text": text, "is_mine": False,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = data_msg.get("attachments", [])
            msg_type, att_info = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": sender, "text": text, "is_mine": False,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info,
            }

        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        if sent:
            text = sent.get("message", "") or ""
            quote = sent.get("quote", {})
            quote_text = quote.get("text", "") if quote else None

            sticker_data = _extract_sticker(sent.get("sticker"))
            if sticker_data:
                msg_type, att_info = sticker_data
                if not text:
                    text = att_info or "🎨 Sticker"
                return {
                    "sender": "Tu", "text": text, "is_mine": True,
                    "quote_text": quote_text, "msg_type": msg_type,
                    "attachment_info": att_info,
                }

            attachments = sent.get("attachments", [])
            msg_type, att_info = _classify_attachments(attachments)
            if not text and attachments:
                text = att_info or "Media"
            return {
                "sender": "Tu", "text": text, "is_mine": True,
                "quote_text": quote_text, "msg_type": msg_type,
                "attachment_info": att_info,
            }

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

    # ─── Processamento envelope ─────────────────────────────────────────────

    def _process_envelope(self, envelope: dict) -> bool:
        """Processa un envelope: identifica il contatto, salva in cache.
        Se il contatto è quello correntemente selezionato, mostra subito il messaggio."""
        contact = self._identify_contact_for_envelope(envelope)
        if contact is None:
            return False

        ts = self._get_message_timestamp(envelope)
        data = self._extract_message_data(envelope)
        if data is None:
            return False

        if contact.number not in self._cache:
            self._cache[contact.number] = []
        self._cache[contact.number].append({
            "text": data["text"],
            "is_mine": data["is_mine"],
            "sender": data["sender"],
            "timestamp": ts,
            "quote_text": data["quote_text"],
            "msg_type": data["msg_type"],
            "attachment_info": data["attachment_info"],
            "read": data["is_mine"],
        })
        _save_cache(self._cache)
        _prune_cache()
        self._cache = _load_cache()

        # Se è il contatto corrente, mostra subito il messaggio nella UI
        if self.selected_contact and contact.number == self.selected_contact.number:
            if ts and ts not in self._seen_timestamps:
                self._seen_timestamps.add(ts)
                self.call_from_thread(
                    self._add_message,
                    data["text"],
                    is_mine=data["is_mine"],
                    quote_text=data["quote_text"],
                    msg_type=data["msg_type"],
                    attachment_info=data["attachment_info"],
                )
        else:
            # Messaggio per un altro contatto: aggiorna badge unread
            self.call_from_thread(self._update_unread_badges)

        return True

    # ─── Startup ────────────────────────────────────────────────────────────

    def _startup(self):
        """Avvia signal-cli daemon e carica i contatti."""
        self._cache = _load_cache()
        _prune_cache()
        self._cache = _load_cache()  # ricarica dopo il prune

        self.call_from_thread(self._add_message, "⏳ Avvio signal-cli daemon...", is_info=True)
        self.rpc = SignalRPCClient()

        if _is_daemon_running():
            self._use_daemon = True
            self.call_from_thread(
                self._add_message, "✅ Daemon già attivo, collegamento diretto...", is_info=True
            )
            self._load_contacts_rpc()

            self._polling_active = True
            self.run_worker(self._poll_worker, exclusive=True, thread=True)
            return

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

        self._polling_active = True
        self.run_worker(self._poll_worker, exclusive=True, thread=True)

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

    def _sort_contacts(self):
        """Ordina i contatti: prima quelli con più non letti, poi alfabetico."""
        self.contacts.sort(
            key=lambda c: (
                -self._unread_counts.get(c.number, 0),
                c.display_name.lower(),
            )
        )

    def _update_contacts_ui(self, contacts: list[Contact]):
        """Aggiorna l'interfaccia con la lista contatti."""
        self._sort_contacts()
        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        for c in self.contacts:
            contact_list.append(ListItem(Label(f"📱 {c.display_name}")))

        self._add_message(f"✅ Caricati {len(contacts)} contatti.", is_info=True)
        self._add_message("💡 Seleziona un contatto per vedere la chat", is_info=True)

        self._update_unread_badges()

    # ─── Selezione contatto ─────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected):
        """Quando un contatto viene selezionato, mostra la chat."""
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

            self.run_worker(
                self._load_messages_worker, exclusive=True, thread=True
            )

            # Refresh finale: recupera messaggi arrivati durante il caricamento
            self._refresh_chat()

            # Segna tutti i messaggi di questo contatto come letti
            number = self.selected_contact.number
            if number in self._cache:
                for msg in self._cache[number]:
                    if not msg.get("read", True):
                        msg["read"] = True
                _save_cache(self._cache)
                _prune_cache()
                self._cache = _load_cache()
            self._unread_counts[number] = 0

            # Forza aggiornamento label per rimuovere badge *N
            contact_list = self.query_one("#contact-list", ListView)
            item = contact_list.children[index]
            item.children[0].update(f"📱 {self.selected_contact.display_name}")

    # ─── Logica messaggi ────────────────────────────────────────────────────

    def _load_messages_worker(self):
        """Carica i messaggi: ultimi 100 dalla cache.
        Se ci sono più di 100 messaggi, mostra un widget per caricare il resto."""
        if not self.selected_contact:
            return

        contact = self.selected_contact
        self._loaded_all = False

        cached = self._cache.get(contact.number, [])
        total = len(cached)

        if cached:
            if total > 20:
                messages_to_show = cached[-20:]
                self.call_from_thread(self._add_load_more_widget, total - 20)
            else:
                messages_to_show = cached
                self._loaded_all = True

            for msg in messages_to_show:
                text = msg.get("text", "")
                is_mine = msg.get("is_mine", False)
                quote_text = msg.get("quote_text")
                ts = msg.get("timestamp", 0)
                msg_type = msg.get("msg_type", "text")
                attachment_info = msg.get("attachment_info")

                if ts:
                    self._seen_timestamps.add(ts)

                self.call_from_thread(
                    self._add_message,
                    text,
                    is_mine=is_mine,
                    quote_text=quote_text,
                    msg_type=msg_type,
                    attachment_info=attachment_info,
                )

            self.call_from_thread(
                self._add_message,
                f"📋 Caricati {len(messages_to_show)}/{total} messaggi",
                is_info=True,
            )
        else:
            self._loaded_all = True
            self.call_from_thread(
                self._add_message, "Nessun messaggio in cronologia per questo contatto", is_info=True
            )

        self.call_from_thread(self._add_message, "✅ Pronto", is_info=True)

    def _add_load_more_widget(self, remaining: int):
        """Aggiunge un widget cliccabile per caricare i messaggi precedenti."""
        chat_log = self.query_one("#chat-log", Vertical)
        widget = Button(
            f"📜 ↑ {remaining} messaggi precedenti — clicca per caricare",
            classes="msg-load-more",
            id="load-more-msg",
        )
        chat_log.mount(widget, before=0)

    def on_button_pressed(self, event: Button.Pressed):
        """Quando l'utente clicca sul pulsante 'carica precedenti'."""
        if event.button.id == "load-more-msg":
            self._load_all_messages()

    def _load_all_messages(self):
        """Carica TUTTI i messaggi dalla cache e ricostruisce la chat."""
        if not self.selected_contact:
            return

        contact = self.selected_contact
        cached = self._cache.get(contact.number, [])

        self._clear_chat()
        self._seen_timestamps.clear()

        for msg in cached:
            text = msg.get("text", "")
            is_mine = msg.get("is_mine", False)
            quote_text = msg.get("quote_text")
            ts = msg.get("timestamp", 0)
            msg_type = msg.get("msg_type", "text")
            attachment_info = msg.get("attachment_info")

            if ts:
                self._seen_timestamps.add(ts)

            self._add_message(
                text,
                is_mine=is_mine,
                quote_text=quote_text,
                msg_type=msg_type,
                attachment_info=attachment_info,
            )

        self._loaded_all = True
        self._add_message(f"📋 Caricati tutti i {len(cached)} messaggi", is_info=True)

    def _poll_worker(self):
        """Thread worker che fa polling ogni 1 secondo.
        Processa TUTTI i messaggi in arrivo e li salva in cache.
        Parte UNA VOLTA in _startup() e vive per tutta l'app."""
        while self._polling_active:
            if self._use_daemon and self.rpc:
                try:
                    messages = self.rpc.receive()
                    for msg in messages:
                        envelope = msg.get("envelope", {})
                        self._process_envelope(envelope)
                except Exception:
                    pass
            for _ in range(10):
                if not self._polling_active:
                    return
                time.sleep(0.1)

    def _refresh_chat(self):
        """Controlla la cache per nuovi messaggi del contatto corrente non ancora mostrati."""
        if not self.selected_contact:
            return

        contact = self.selected_contact
        cached = self._cache.get(contact.number, [])
        nuovi = 0

        for msg in cached:
            ts = msg.get("timestamp", 0)
            if ts and ts not in self._seen_timestamps:
                self._seen_timestamps.add(ts)
                text = msg.get("text", "")
                is_mine = msg.get("is_mine", False)
                quote_text = msg.get("quote_text")
                msg_type = msg.get("msg_type", "text")
                attachment_info = msg.get("attachment_info")
                self._add_message(
                    text,
                    is_mine=is_mine,
                    quote_text=quote_text,
                    msg_type=msg_type,
                    attachment_info=attachment_info,
                )
                nuovi += 1

        if nuovi > 0:
            chat_log = self.query_one("#chat-log", Vertical)
            chat_log.scroll_end(animate=False)

    def _update_unread_badges(self):
        """Controlla la cache in memoria e aggiorna i badge *N sui contatti.
        Se i conteggi cambiano, riordina la lista e la ricostruisce."""
        if not self.contacts:
            return

        changed = False
        for contact in self.contacts:
            messages = self._cache.get(contact.number, [])
            unread = sum(
                1 for m in messages
                if not m.get("is_mine") and not m.get("read", True)
            )
            old = self._unread_counts.get(contact.number, 0)
            if unread != old:
                self._unread_counts[contact.number] = unread
                changed = True

        if not changed:
            return

        # Riordina e ricostruisce la lista
        self._sort_contacts()
        contact_list = self.query_one("#contact-list", ListView)
        contact_list.clear()
        for c in self.contacts:
            label = f"📱 {c.display_name}"
            unread = self._unread_counts.get(c.number, 0)
            if unread > 0 and c != self.selected_contact:
                label += f" *{unread}"
            contact_list.append(ListItem(Label(label)))

        # Ripristina la selezione sul contatto corrente (se presente)
        if self.selected_contact and self.selected_contact in self.contacts:
            contact_list.index = self.contacts.index(self.selected_contact)

    # ─── Invio messaggi ─────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted):
        """Invia un messaggio quando l'utente preme Invio."""
        if not self.selected_contact:
            self._add_message("❌ Seleziona prima un contatto!", is_info=True)
            return

        message = event.value.strip()
        if not message:
            return

        number = self.selected_contact.number
        ts = int(time.time() * 1000)
        if number not in self._cache:
            self._cache[number] = []
        self._cache[number].append({
            "text": message,
            "is_mine": True,
            "sender": "Tu",
            "timestamp": ts,
            "quote_text": None,
            "msg_type": "text",
            "attachment_info": None,
            "read": True,
        })
        _save_cache(self._cache)
        _prune_cache()
        self._cache = _load_cache()

        # Mostra subito il messaggio nella UI
        self._add_message(message, is_mine=True)

        event.input.value = ""

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


if __name__ == "__main__":
    import signal as signal_module

    app = SignalTUI()

    def _handle_sigint(sig, frame):
        """Gestisce Ctrl+C: ferma il polling ed esce pulitamente."""
        app._polling_active = False
        app.exit()

    signal_module.signal(signal_module.SIGINT, _handle_sigint)
    app.run()
