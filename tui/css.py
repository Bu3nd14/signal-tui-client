APP_CSS = """
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

#ChatTitle {
    text-align: left;
}

#contact-list {
    height: 100%;
    border: solid $accent;
}

#contact-list.chat-filter-signal {
    border: solid #3b82f6;
}

#contact-list.chat-filter-whatsapp {
    border: solid #25d366;
}

#contact-list.chat-filter-telegram {
    border: solid #0088cc;
}

#contact-list.chat-filter-unread {
    border: solid #f59e0b;
}

#contact-list ListItem {
    padding: 1 1;
}

#contact-list ListItem.contact-group {
    text-style: bold;
    padding: 0 1;              /* header più compatto */
}

#contact-list ListItem.contact-member {
    padding: 0 1 0 3;          /* indentazione sotto l'header */
}

#contact-list ListItem:hover {
    background: $accent 20%;
}

#contact-list ListItem:focus {
    background: $accent 40%;
}

/* La riga selezionata (classe `-highlight`) usa SEMPRE il colore "blurred",
   anche quando la ListView ha il focus: così il colore non cambia tra il
   primo click (focus torna all'input) e il secondo click (focus resta sulla
   ListView). */
#contact-list ListItem.-highlight,
#contact-list:focus ListItem.-highlight {
    color: $block-cursor-blurred-foreground;
    background: $block-cursor-blurred-background;
    text-style: $block-cursor-blurred-text-style;
}

/* Anche lo sfondo dell'intera lista non deve schiarirsi quando la ListView
   prende il focus (2° click sullo stesso contatto): il tint di default di
   Textual `$foreground 5%` viene annullato. */
#contact-list:focus {
    background-tint: transparent;
}

/* Protocol accents in the contact list */
.protocol-signal {
    color: #b5c9a8;
}

.protocol-whatsapp {
    color: #4a9e63;
}

.protocol-telegram {
    color: #1a8a4a;
}

#contact-list .protocol-signal:hover,
#contact-list .protocol-whatsapp:hover,
#contact-list .protocol-telegram:hover {
    color: $text;
}

#chat-log {
    height: 1fr;
    border: solid $accent;
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

.msg-pending {
    color: $text-muted;
}

.msg-failed {
    color: $error;
}

.msg-info {
    text-align: left;
    padding: 0 1;
    color: $text-muted;
}

/* Colore del bordo della chat in base al filtro Ctrl+W (azzurro Signal,
   verde WhatsApp, default/giallo per ALL).  Non usiamo più una "barra"
   laterale (border-left) su ogni messaggio. */
#chat-log.chat-filter-signal {
    border: solid #3b82f6;
}

#chat-log.chat-filter-whatsapp {
    border: solid #25d366;
}

#chat-log.chat-filter-telegram {
    border: solid #0088cc;
}

#chat-log.chat-filter-unread {
    border: solid #f59e0b;
}

/* Banner (titoli di sezione) sincroni col bordo della chat per filtro. */
#ContactsTitle.chat-filter-signal,
#ChatTitle.chat-filter-signal {
    background: #3b82f6;
}

#ContactsTitle.chat-filter-whatsapp,
#ChatTitle.chat-filter-whatsapp {
    background: #25d366;
}

#ContactsTitle.chat-filter-telegram,
#ChatTitle.chat-filter-telegram {
    background: #0088cc;
}

#ContactsTitle.chat-filter-unread,
#ChatTitle.chat-filter-unread {
    background: #f59e0b;
}
.msg-quote {
    text-align: left;
    padding: 0 1 0 3;
    color: $text-muted;
    text-style: italic;
}

.msg-quote-right {
    text-align: right;
    padding: 0 3 0 1;
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

#reply-bar {
    height: auto;
    padding: 0 1;
    background: $accent 30%;
    color: $text;
    text-style: bold;
    border: solid $accent;
    margin: 0 1;
}

#reply-bar.reply-bar-hidden {
    display: none;
}

#reply-text {
    width: 1fr;
    padding: 0 1;
}

.reply-cancel-btn {
    width: 3;
    text-align: center;
    color: $error;
    text-style: bold;
    background: transparent;
    border: none;
    padding: 0;
    min-width: 3;
}

.reply-cancel-btn:hover {
    background: $error 30%;
}

#input-row {
    dock: bottom;
    height: 3;
    margin: 1 0;
}

#emoji-btn {
    width: 6;
    min-width: 6;
    margin: 0 0 0 1;
    content-align: left middle;
    padding: 0;
    border: tall $border;
    background: $surface;
    color: $text;
}

#emoji-btn:hover {
    background: $accent 30%;
}

#message-input {
    width: 1fr;
    height: 3;
    margin: 0 1 0 0;
    border: tall $border;
    overflow-y: auto;
}

Horizontal {
    height: 1fr;
}

Footer {
    dock: none;
    width: 1fr;
}

#bottom-bar {
    dock: bottom;
    height: 1;
    background: $footer-background;
}

#status-bar {
    width: 45;
    align-horizontal: right;
    color: $footer-foreground;
}

#status-bar StatusSegment {
    width: auto;
    padding: 0 1;
}

#status-bar StatusSegment:hover {
    background: $boost;
}

#status-bar StatusSegment.status-segment-active {
    background: $accent 40%;
    color: $text;
    text-style: bold;
}

#status-bar #status-text {
    width: auto;
    text-align: right;
}
"""
