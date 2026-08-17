# Bug Report — Signal TUI Client

> **Stato:** Revisionato il 17/08/2026 — verifica conclusiva manuale del codice.
> **Ordinamento:** Per impatto sull'utente finale (dal più grave al meno grave).
> **Nota:** I bug #2, #14, #15 (pattern `_save_cache/_prune_cache/_load_cache` su JSON) sono stati **rimossi** — il passaggio a SQLite li ha resi obsoleti.

---

## 🔴 Critici (impatto diretto sull'esperienza utente)

### #21 — `_mount_window` omette `attachment_path` obbligatorio → TypeError silenzioso (`signal_tui.py`, riga 1540) ✅ RISOLTO

La chiamata `ImageWidget(attachment_id=..., fallback_text=...)` ometteva il parametro
obbligatorio `attachment_path` (che non ha default). Il `TypeError` veniva ingoiato
alla chat. **Nessun placeholder visibile per le immagini caricate da cache.**

**Fix:** aggiunto `attachment_path=None` esplicito nella chiamata. Aggiunto test
`test_image_messages_mount_from_cache` in `test_refresh_chat.py` (370/370 ✅).

**Impatto prima del fix:** Le immagini nei messaggi caricati da cache (riapertura
chat, riavvio app) non mostravano alcun placeholder. Solo i messaggi live (via
`_render_image_in_chat`) funzionavano. Scoperto durante l'analisi del bug #1.

**Root cause del mancato rilevamento:** I test esistenti usavano solo `msg_type="text"`;

---

### #1 — `_classify_attachments` processa solo il primo attachment (`backends/signal.py`, righe 350-369) ✅ RISOLTO

Il `for att in attachments` itera ma fa `return` al primo elemento che matcha.
Se ci sono più attachment (es. un'immagine + un video), solo il primo viene processato.
Inoltre il `return ("attachment", "📎 File", None)` finale (riga 369) era **dead code**
perché il loop ritornava sempre al primo giro.

**Fix:** `_classify_attachments` ora accumula tutti gli attachment in una lista.
`_extract_message_data` restituisce `list[dict]` (un dict per attachment). Il testo
produce N `ChatEvent`. Il chiamante (`_sse_listener`) accoda tutti gli eventi.
Aggiunti 7 test (`test_backends.py`) per: singolo, multipli image, misti
(image+video+audio), testo+attachment, solo testo, sentMessage multipli, envelope
vuoto. 377/377 ✅.

**Impatto prima del fix:** Media allegati persi — l'utente non vedeva attachment multipli.

---

## 🟠 Alti (sicurezza, integrità o perdita di messaggi)

### #25 — Webhook WhatsApp multi-allegato perde tutti gli elementi dopo il primo (`backends/whatsapp_events.py`, righe 271-302; `backends/whatsapp.py`, righe 221-231)

Gli eventi generati per gli attachment di uno stesso messaggio riusano `msg_id`.
Il dedup del backend considera quindi duplicati gli eventi successivi e conserva
solo il primo allegato.

**Scenario:** messaggio WhatsApp con più attachment nell'array `attachments`.

**Fix suggerito:** assegnare un id evento univoco per attachment (ad esempio
`msg_id` più indice/id media) e mantenere un id comune separato per il messaggio.

---

### #26 — Download server esposto sulla LAN senza controllo d'accesso (`backend/download.py`, righe 61-72, 91-94)

Il server degli attachment ascolta su `0.0.0.0`; `SimpleHTTPRequestHandler` espone
la directory temporanea e gli URL non richiedono autenticazione o token.

**Scenario:** un host della LAN raggiunge la porta di download e scarica attachment
o enumera i file serviti.

**Fix suggerito:** bind su loopback salvo opt-in esplicito; se serve accesso remoto,
usare URL con token non prevedibile, disabilitare directory listing e applicare TTL.

---

### #27 — Webhook WhatsApp accetta POST non autenticati dalla rete (`backend/webhook.py`, righe 39-59, 93-97)

Il listener WAHA è in bind su `0.0.0.0` e inoltra qualsiasi JSON ricevuto su
`/webhook`, senza firma, token o altra autenticazione.

**Scenario:** un host raggiungibile invia un POST artefatto e inserisce falsi
messaggi/eventi nella TUI.

**Fix suggerito:** bind su loopback per default e validare una firma HMAC o un token
segreto prima di inoltrare il payload.

---

### #28 — Signal scarta i messaggi di mittenti non presenti nei contatti (`backends/signal.py`, righe 366-400, 605-608)

`_identify_contact_for_envelope` restituisce `None` se il mittente non è nella lista
contatti; `envelope_to_event` interrompe quindi l'elaborazione e il messaggio va perso.

**Scenario:** ricezione da un nuovo numero o da un contatto non ancora sincronizzato.

**Fix suggerito:** creare/aggiornare un contatto provvisorio dal mittente dell'envelope
oppure conservare il messaggio finché la rubrica non è aggiornata.

---

## 🟡 Medi (funzionalità degradate)

### #6 — Errori del polling eliminano lo sleep e il retry SSE è a pausa fissa (`tui/polling.py`, righe 18-100; `backends/signal.py`, righe 761-782)

Le eccezioni in `_poll_worker` raggiungono l'`except` esterno prima dello sleep:
il ciclo riparte subito, causando hot loop e log ripetuti. Il listener SSE ritenta
inoltre sempre dopo una pausa fissa, senza comunicare all'utente lo stato degradato.

**Scenario:** daemon o rete indisponibili per più tentativi consecutivi.

**Fix suggerito:** applicare un backoff bounded con reset dopo successo e mostrare
una notifica di connessione/ricezione degradata.

---

### #9 — Ricerca emoji non indicizza gli alias alternativi (`emoji_picker.py`, righe 35-54)

L'indice di ricerca conserva solo il nome canonico di ogni emoji, non tutti gli alias.
Per esempio `:smile:` è sostituibile ma non ricercabile se l'emoji è indicizzata con
un altro nome.

**Impatto:** Ricerca/autocomplete emoji incompleta.

**Fix suggerito:** indicizzare tutti gli alias per emoji, mantenendo un risultato
unico quando più alias corrispondono.

---

### #18 — Ogni nuovo download invalida gli URL precedenti (`backend/download.py`, righe 102-139, 175-181)

`_clean_download_dir()` elimina tutti i file serviti prima di pubblicarne uno nuovo.
Un URL già consegnato può quindi restituire 404 quando viene richiesto dopo un altro
download.

**Scenario:** l'utente apre due download in successione o condivide il primo URL.

**Fix suggerito:** usare nomi univoci e retention temporale/per sessione, rimuovendo
solo i file scaduti.

---

### #24 — Scadenze typing e mumbling dipendono dall'arrivo di eventi (`tui/polling.py`, righe 33-65)

I timeout vengono valutati solo all'interno del `for event in events`. Se non arrivano
altri eventi, typing e mumbling non scadono e lo stato resta visibile indefinitamente.

**Fix suggerito:** valutare le scadenze a ogni ciclo di polling o tramite timer
separato, anche con batch vuoti.

---

### #29 — Le reply Telegram sono visualizzate ma inviate come messaggi normali (`backends/telegram.py`, righe 333-378)

I parametri della quote sono accettati ma la chiamata a `send_message` non passa
`reply_to`. La UI cita il messaggio, mentre Telegram riceve un messaggio senza reply.

**Fix suggerito:** risolvere l'id del messaggio quotato e passarlo come `reply_to`

---

### #30 — Reply Signal a un proprio messaggio usa l'autore della controparte (`tui/send.py`, righe 167-173)

Per ogni reply il worker usa `contact.id` come `quote_author`. Quando il messaggio
citato è dell'utente locale, Signal riceve invece l'autore della controparte.

**Scenario:** rispondere a un proprio messaggio nella chat Signal.

**Fix suggerito:** conservare l'autore/is_mine nei dati della reply e usare l'identità
dell'account locale per messaggi propri.

---

### #32 — Le foto Telegram dello storico non sono scaricabili né apribili (`backends/telegram.py`, righe 411-427, 460-466, 482-500)

Il download della foto avviene solo nel gestore live. Lo storico costruisce il
placeholder senza path e, diversamente dai documenti, senza un identificatore
utile al download lazy/on-demand.

**Scenario:** aprire una chat con foto Telegram caricate dalla cronologia.

**Fix suggerito:** scaricare le foto anche durante il fetch dello storico o conservare
file/reference id e implementare il download lazy al click.

---

### #5 — `_identify_contact_for_envelope` logica duplicata per `sent` (`backends/signal.py`, righe 320-351) ✅ RISOLTO

**Fix:** rimosso il secondo blocco `sent` ridondante (cercava solo `dest` senza
`dest_number`/`dest_uuid`). Aggiunto `return None` esplicito dopo il primo blocco
per evitare che un envelope `sentMessage` senza match cada nella ricerca per `source`
(che per un envelope `sent` è l'utente locale, non un contatto reale).

---

## 🟢 Minori (comportamenti subottimali ma non bloccanti)

### #34 — Invii deduplicati possono lasciare una seconda bolla pending senza riga DB (`tui/send.py`)

Due invii dello stesso testo entro la finestra di dedup possono creare una seconda
bolla ottimistica mentre l'insert SQLite viene deduplicato. La finalizzazione aggiorna
UI e cache solo se l'update DB trova una riga: la seconda bolla resta quindi `pending`
senza record persistito.

**Scenario:** inviare due volte rapidamente lo stesso testo alla stessa chat entro la
finestra di deduplicazione.

**Fix suggerito:** rendere la transizione di UI/cache indipendente dall'insert
deduplicato, oppure impedire la creazione della seconda bolla e del relativo worker.

---

### #10 — Il picker emoji duplica la ricerca e omette risultati (`emoji_picker.py`, righe 347-374)

`on_input_changed` reimplementa la ricerca invece di usare `search_emoji()`. Cerca
solo le 1.081 emoji delle categorie, contro le 5.225 considerate da `search_emoji()`,
perciò alcuni risultati non compaiono nel picker.

**Fix suggerito:** riusare `search_emoji()` o un indice condiviso per ricerca e
autocomplete.

---

### #11 — Output stdout vuoto di catimg apre una modale vuota (`ui_components.py`)

Se `catimg` non produce stdout (ad esempio per un file corrotto), la modale renderizza
testo ANSI vuoto senza errore visibile.

**Fix suggerito:** controllare `ansi_output.strip()` e mostrare un messaggio d'errore
esplicito quando l'output è vuoto.

---

### #31 — `ImageWidget` ricostruito dalla cache perde classe e allineamento (`tui/chat_view.py`, righe 541-551)

Il ramo cache crea `ImageWidget` senza assegnare `msg-left` o `msg-right`, a differenza
del rendering live. Immagini ricaricate perdono quindi allineamento e colore previsti.

**Fix suggerito:** assegnare la classe in base a `is_mine`, come nel percorso live.

---

### #16 — `_parse_contacts_from_output` parsing fragile (`backends/signal.py`, righe 177-195) ✅ RISOLTO

**Fix:** sostituito `line.split()` con regex `_RE_CONTACT_LINE` che usa named groups. Ora
gestisce correttamente nomi con spazi (es. "Mario Rossi"). Commit: (vedi git log).

---

### #33 — Stream SSE Signal senza eventi causa `UnboundLocalError` (`backends/signal.py`, righe 761-775)

Dopo il `for envelope in ...`, il listener valuta `if envelope:`. Se lo stream termina
senza produrre elementi, `envelope` non è definita e l'errore viene mascherato come
connection lost.

**Fix suggerito:** rimuovere il controllo finale oppure inizializzare la variabile e
strutturare il loop senza dipendere dall'ultimo elemento iterato.

---

## 🗑️ Bug rimossi (obsolescenza confermata)

| # | Descrizione | Motivo |
|---|-------------|--------|
| #2 | `_process_envelope` salva/ricarica cache ridondantemente | Funzione non esiste più — envelope parsing in `backends/signal.py` usa SQLite |
| #14 | `on_list_view_selected` salva/ricarica cache ridondantemente | Pattern `_save_cache`/`_load_cache` rimosso con SQLite |
| #15 | `on_input_submitted` salva/ricarica cache ridondantemente | Pattern `_save_cache`/`_load_cache` rimosso con SQLite |

## ✅ Bug risolti (storico)

| # | Descrizione | Fix e verifica |
|---|-------------|----------------|
| #22 | Il worker di invio poteva rileggere `selected_contact` e inviare alla chat sbagliata | `protocol` e `contact_id` vengono catturati al submit e passati al worker; aggiunti test di regressione |
| #23 | L'invio ottimistico restava inviato e persistito dopo un errore di rete | Introdotti gli stati `pending`, `sent` e `failed`; DB, cache e UI sono aggiornati atomicamente. Il retry conserva destinatario e contenuto e riusa la riga esistente senza duplicarla. Fix validata. |
