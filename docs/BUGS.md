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

### #30 — Reply Signal a un proprio messaggio usa l'autore della controparte (`tui/send.py`, righe 167-173)

Per ogni reply il worker usa `contact.id` come `quote_author`. Quando il messaggio
citato è dell'utente locale, Signal riceve invece l'autore della controparte.

**Scenario:** rispondere a un proprio messaggio nella chat Signal.

**Fix suggerito:** conservare l'autore/is_mine nei dati della reply e usare l'identità
dell'account locale per messaggi propri.

---

### #37 — Quote di un'immagine: non visibile in ingresso e impossibile da creare dalla TUI (`tui/chat_view.py`; `tui/send.py`; backends)

Quando un contatto quota un'immagine da un altro client, la TUI non mostra il
quoting (la bolla quote risulta vuota/assente). Nello stesso tema, non esiste un
modo per **quotare un'immagine dalla TUI**: il click su un'immagine la apre
(modal `ImageModalScreen`) e il flusso di reply/quote è pensato solo per i
messaggi di testo (click sul `MessageWidget` → reply).

**Scenario:** ricevere una reply a un'immagine (es. da Signal) oppure voler
rispondere quotando una foto dall'interno della TUI.

**Verifiche:** quote in ingresso confermato NON visibile su **Signal**; da
verificare per **WhatsApp** e **Telegram**. Da capire anche l'interazione di
input per la creazione (es. tasto dedicato / modifica del comportamento del
click, che oggi apre l'immagine).

**Fix suggerito:** rendere il quoting visibile anche per i messaggi media
(caption/nome file nel `quote_text`), e aggiungere un'azione "quote/reply" per le
immagini senza rompere l'apertura in modal (es. scorciatoia dedicata o menu),
propagando `reply_to_message_id`/quote anche per i media.

---

### #38 — Lista principale non aggiornata dopo la risoluzione `@lid` in background: WhatsApp non raggruppato fino a riavvio (`backends/whatsapp.py` `_lid_resolver_run`; `_load_contacts`)

Il raggruppamento dei contatti per persona fonde WhatsApp con Signal/Telegram
solo se il contatto WhatsApp ha il numero in `extras["phone"]` (derivato dall'id
`@c.us` o dal lookup in `_lid_map` per gli `@lid`). Al primo avvio con cache
fredda (`wa_lid_map.json` vuota), `_load_contacts` costruisce `self.contacts`
PRIMA che il resolver in background (`start_lid_resolver`, avviato
automaticamente alla connessione) popoli la mappa: i contatti `@lid` restano
caricati senza phone e la lista principale li mostra come gruppi single-member
non fusi. `_lid_resolver_run` salva la mappa su disco ma NON ricostruisce
`self.contacts` né ri-renderizza la lista: il raggruppamento WhatsApp compare
solo al riavvio/riconnessione successivi (quando `_load_contacts` legge la mappa
persistita).

**Scenario:** primo avvio con cache lid fredda — i contatti WhatsApp non risultano
raggruppati con Signal/Telegram per tutta la sessione, nonostante il resolver sia
già partito in background.

**Impatto:** nessuna perdita dati (dal secondo avvio il raggruppamento è
automatico), ma la prima esecuzione richiede un riavvio per vedere WhatsApp fuso.
Colpisce solo chi non ha mai aperto il picker (Ctrl+S) prima.

**Fix suggerito:** al termine di `_lid_resolver_run` (o su notifica), rieseguire
`_load_contacts()` e ri-renderizzare la lista (es. `_render_contact_list`) per i
contatti il cui `@lid` è stato risolto; oppure far ripartire la proiezione
`_visible_rows()` su `self.contacts` con le extras aggiornate.

---

### #39 — Messaggio WhatsApp inviato resta "grigio" (pending) in UI fino a un receipt successivo (`tui/send.py` `_transition_outgoing_status`; `backends/whatsapp_events.py` `_ack_value`/`_event_from_ack`) ✅ RISOLTO

Inviando un messaggio WhatsApp dalla TUI, la bolla ottimistica resta nello stato
`pending` (CSS `.msg-pending` = `$text-muted`, "grigio") invece di avanzare a
`sent`, e si corregge solo quando arriva un receipt successivo (es. il
destinatario legge il messaggio per rispondere). Il DB invece raggiunge lo stato
finale corretto (`delivered`): il problema è solo la **bolla live** che non riceve
l'aggiornamento in tempo.

Due concause emerse dall'analisi:
1. **Transizione pending→sent con early-return silenzioso**: `_transition_outgoing_status`
   (→ `_update_message_status` su SQLite) ritorna `False` **senza aggiornare né DB,
   né cache UI, né widget** se la riga DB non viene trovata (o lo stato non è più
   `pending`). La bolla resta pending senza alcun log e si sblocca solo quando un
   receipt per `msg_id` raggiunge il widget.
2. **Mappatura ack WAHA divergente dalla docu ufficiale**: l'app usa la propria enum
   (2=SERVER_ACK, 3=DELIVERY_ACK, 4=READ) mentre la docu WAHA dichiara
   (1=SERVER, 2=DEVICE→consegnato, 3=READ, 4=PLAYED). Di conseguenza il receipt di
   *consegna* (DEVICE/ack=2) viene ignorato (`status < 3`) e il *read* (ack=3) viene
   trattato come `delivered` — gli aggiornamenti intermedi di stato arrivano in modo
   sporadico, aggravando il "grigio".

**Scenario:** inviare un messaggio a un contatto WhatsApp dalla TUI: resta grigio
finché il destinatario non legge/risponde.

**Verifiche:** DB reale con stato finale `delivered` (corretto) ma senza traccia di
uno scatto a `sent` al momento dell'invio; "grigio" corrisponde alla classe
`.msg-pending`.

**Fix proposto B (preventivo, preferito):** allineare la mappatura ack a quella
ufficiale WAHA in `_ack_value`/`_event_from_ack`/`process_receipt` (1=SERVER
ignorato, 2=DEVICE→`delivered`/is_read=False, 3=READ→`read`/is_read=True, 4=PLAYED
solo vocali), verificando i valori reali emessi dalla build WAHA in uso.

**Aggiunta possibile A (strumentazione):** log in `_transition_outgoing_status`
quando ritorna `False` (contact_id, ts, text, esito di `_update_message_status`) e
in `_send_message_worker` (`added`, `persist`, `result`); log dei payload
`message.ack` reali in `handle_webhook` (valori `ack`/`ackName`) per confermare la
causa del mancato passaggio pending→sent prima o insieme al fix B.

**Fix (branch `fix/wa-receipt-id-match`, mergeato):**
- **Fix B applicato** con il #41 (enum ack ufficiale WAHA: 1=SERVER ignorato,
  2=DEVICE→delivered, 3=READ→read).
- **Root cause del match fallito (scoperta sul campo, 21/08/2026):** WAHA
  2026.8.1 (WEBJS) usa formati di id diversi a seconda del canale — DB/cache
  `true_{jid}_{hex}_{participant@lid}` (gruppi, con partecipante) vs payload ack
  senza partecipante (hex a volte troncato). `process_receipt` matchava per id
  **esatto** → il receipt non trovava mai la bolla (nel DB reale: 66 messaggi DM
  bloccati a `sent`). Il fix #41 aveva amplificato il fenomeno abbassando la
  soglia ack (`>=2`), generando più receipt che tentavano match falliti.
- **Fix applicato:** nuova funzione pura `canonical_msg_id()` in
  `backends/whatsapp_events.py` (estrae l'hex canonico da tutte le forme note,
  validata sui 1124 id reali del DB: 0 errori, 0 collisioni); `_event_from_ack`
  normalizza l'id nel payload del receipt; `process_receipt` confronta per id
  canonico con **fallback per unicità** (una sola entry `is_mine` `sent` id-less
  nella chat) e **logger.warning** solo su vero mismatch (niente rumore sui
  no-op da rank-guard). Strumentazione in `_transition_outgoing_status`
  (`logger.warning` su early-return False).
- Test: `tests/test_whatsapp_receipt_id_match.py` (20 test). Suite completa:
  1174 passed. Lint/format puliti.

**Fix aggiuntivo (21/08/2026, branch `fix/outgoing-status-fallback`):** su
produzione è emersa la **race condition pendente→sent**: l'echo di WAHA (spesso
più veloce del worker) può sostituire il timestamp ottimistico del client con
quello del server PRIMA della transizione → `_update_message_status` (match per
`timestamp`) fallisce e la bolla resta grigia finché un receipt successivo la
corregge (es. chat Tartufi Bolliti, `ts client 1787342685618` vs `ts server
1787342685000`). Fix: nuova `_update_message_status_by_text` (aggiorna la riga
outgoing più recente per testo, con expected-status e rank guard) usata come
fallback in `_transition_outgoing_status`; cache backend/UI aggiornate per
testo+is_mine quando il timestamp non combacia. Test:
`tests/test_outgoing_status_fallback.py` (7 test). Suite completa: 1181 passed.

---

### #40 — Indicatore di digitazione (typing) non funzionante per Telegram e WhatsApp, solo per Signal (`backends/telegram.py`; `backends/whatsapp.py`/`backends/whatsapp_events.py`; `backends/signal.py`, righe 759-764) ✅ RISOLTO (Telegram) / 🚫 WON'T FIX (WhatsApp)

L'indicatore "✍️ sta scrivendo" (stato gestito in `tui/events.py` `_handle_typing_event`
e `tui/polling.py`) dipende dagli eventi `ChatEvent(type="typing")` emessi dai
backend. Verifica per protocollo:

- **Signal** — funziona: `_process_typing(envelope)` estrae il campo `typingMessage`
  e `envelope_to_event` emette `ChatEvent(type="typing")` (`backends/signal.py`,
  righe 759-764).
- **Telegram** — nessuna pipeline: `backends/telegram.py` emette solo eventi
  `message`/`message_edit`/`receipt` e non registra alcun handler di digitazione
  (es. `UpdateUserTyping`/`UpdateChatUserTyping`/`UpdateChannelUserTyping` di
  Telethon). Nessun evento typing viene mai prodotto.
- **WhatsApp** — la normalizzazione esiste (`_event_from_typing` in
  `backends/whatsapp_events.py`, righe 433-454, gestisce `typing`/
  `presence.update`/`presence`) ed è inoltrata da `handle_webhook`, ma gli
  indicatori non arrivano alla TUI: da verificare il payload reale emesso da WAHA
  (flag/valori `presence`/`typing`, es. "composing"/"paused") e l'eventuale
  sottoscrizione al flag typing sul webhook.

**Scenario:** l'utente di una chat Signal/Telegram/WhatsApp digita mentre si
osserva la lista contatti: l'indicatore ✍️ compare solo per i contatti Signal.

**Verifiche:** nessun evento `type="typing"` nei log per Telegram/WhatsApp; test
esistenti solo su Signal (`tests/test_backends.py::test_envelope_to_event_typing`)
e sulla normalizzazione unitaria WhatsApp (`tests/test_whatsapp_backend.py::test_typing_event`),
nessun test end-to-end del flusso per i due protocolli mancanti.

**Fix suggerito:** per Telegram implementare la ricezione degli update di
digitazione MTProto (es. `events.Raw` su `UpdateUserTyping`/`UpdateChatUserTyping`/
`UpdateChannelUserTyping`) normalizzandoli in `ChatEvent(type="typing")`; per
WhatsApp verificare configurazione WAHA e payload `presence.update` reali e
allineare `_event_from_typing`; aggiungere test end-to-end per entrambi i backend.

**Fix** (branch `fix/typing-delivered-40-41`, design in `DESIGN_FIX_40_41.md`):
- **Telegram:** nuovo traduttore `_handle_typing_update` (`backends/telegram.py`)
  che converte `UpdateUserTyping`/`UpdateChatUserTyping`/`UpdateChannelUserTyping`
  in `ChatEvent(type="typing")` (action STARTED/STOPPED), con le stesse
  convenzioni `contact_id` di `_handle_read_receipt`; dispatch esteso nel ramo
  `events.Raw` di `_on_raw`. Mapping: `SendMessageCancelAction` → STOPPED, tutte
  le altre `SendMessage*Action` → STARTED.
- **WhatsApp** (3 interventi coordinati): (1) `presence.update` aggiunto agli
  eventi sottoscritti in `_configure_webhook` e a `WAHA_WEBHOOK_EVENTS` in
  `docker-compose.yml`; (2) `_event_from_typing` allineato alla shape ufficiale
  WAHA `payload.presences[].lastKnownPresence` (+ fallback legacy) con filtro
  obbligatorio `online`/`offline`/`unavailable` → `None` (niente 💭 spurio);
  (3) subscribe per-chat `POST /api/{session}/presence/{chatId}/subscribe`
  (`backends/whatsapp_rest.py` `presence_subscribe`), sweep al connect +
  subscribe lazy su `fetch_history`/primo messaggio.
- Test: classe `TestTelegramTyping` (8 test) in `tests/test_telegram.py`;
  `tests/test_whatsapp_fix_40_41.py` (25 test). Suite completa: 1154 passed.

**🚫 WON'T FIX (WhatsApp) — limite dell'engine WEBJS di WAHA.** Il typing
WhatsApp non è ottenibile con lo stack attuale, verificato il 21/08/2026 su
**WAHA 2026.8.1 (tier CORE, engine WEBJS)**:

- La subscribe per-chat `POST /api/{session}/presence/{chatId}/subscribe` risponde
  **HTTP 500** per ogni JID: `TypeError: d(...).subscribePresence is not a
  function` — il metodo **non esiste** nel core WEBJS di WAHA
  (`WebjsClientCore.js`). L'API della pagina WhatsApp Web usata da
  whatsapp-web.js non espone `subscribePresence`.
- Senza subscribe per-chat WAHA **non distribuisce** gli eventi `presence.update`
  (0 eventi nei log, anche dopo il fix config webhook con `presence.update` e
  `WHATSAPP_HOOK_EVENTS`).
- Provato anche `config.webjs.tagsEventsOn: true` (flag che la docu WAHA dichiara
  *required* per `presence.update`): la sessione si riavvia correttamente ma la
  subscribe resta rotta → nessun evento presence.
- Il supporto presence/typing completo è implementato solo sull'engine **NOWEB**
  (senza browser, WebSocket diretto), che richiede il **re-link** della sessione
  (scan QR) e l'abilitazione dello **store** (`config.noweb.store.enabled`) per
  contatti/chats/storico — scelta **non adottata**.

Il codice implementato (subscribe per-chat best-effort, `_event_from_typing`
allineato alla shape ufficiale, `presence.update` nel webhook e nel compose) è
**mantenuto**: diventa operativo senza modifiche se WAHA fixa `subscribePresence`
su WEBJS o se si adotta l'engine NOWEB. Il fallimento resta silenzioso
(best-effort) e non degrada altre funzionalità WhatsApp.

**Follow-up perf (21/08/2026):** lo **sweep presence al connect** (N chat × POST
fallite + pausa 0.3s) e le **lazy subscribe** per messaggio/ack generavano
centinaia di richieste 500 inutili verso WAHA e thread extra nel processo TUI
(con l'endpoint rotto sono lavoro sprecato, percepibile come rallentamento —
bolla "grigia" più a lungo anche su altri protocolli). La subscription è ora
**disabilitata per default** (`_PRESENCE_SUBSCRIBE_ENABLED=False` in
`backends/whatsapp.py`), riabilitabile con l'env `WAHA_PRESENCE_SUBSCRIBE=1`
(es. passando a NOWEB). `_event_from_typing` resta attivo. Test aggiornati
(+1: `test_disabled_by_default_noop`); suite completa 1174 passed.

---

### #41 — Stato "consegnato" mai mostrato per WhatsApp e Telegram: si passa direttamente da "sent" a "letto" (`backends/whatsapp_events.py` `_event_from_ack`, righe 377-431; `backends/telegram.py` `_handle_read_receipt`, righe 938-976) ✅ RISOLTO

Lo stato intermedio `delivered` (grassetto in UI, `ui_components.py`) non viene
mai mostrato per i messaggi inviati via WhatsApp e Telegram: la bolla passa
direttamente da `sent` a `read`. Su Signal il flusso completo
sent → delivered → read funziona (`receiptMessage` con `is_delivery`/`is_read`,
`backend/rpc.py` `_process_receipt`).

- **WhatsApp** — `_event_from_ack` assume l'enum Baileys
  (2=SERVER, 3=DELIVERY→delivered, 4=READ). Se la build WAHA in uso emette i
  valori della docu ufficiale (1=SERVER, 2=DEVICE→consegnato, 3=READ, 4=PLAYED),
  l'ack di consegna (2) viene scartato (`status < 3` → `None`, righe 422-423) e
  il primo receipt utile è il read: la bolla salta `delivered`. **Stessa root
  cause del bug #39** (mappatura ack divergente da quella ufficiale WAHA).
- **Telegram** — `_handle_read_receipt` gestisce esclusivamente
  `UpdateReadHistoryOutbox` ed emette solo eventi `receipt` con
  `is_read=True`. Nessun evento `delivered` viene mai generato (né da MTProto
  né sintetico): `process_receipt` supporta il target `"delivered"` (righe
  992-1000) ma non c'è alcun percorso che lo emetta.

**Scenario:** inviare un messaggio e osservare la bolla: su Signal compare
"consegnato" prima di "letto"; su WhatsApp e Telegram si passa direttamente da
"inviato" a "letto" (o si resta "inviato" se il receipt di lettura non arriva).

**Fix suggerito:** per WhatsApp allineare la mappatura ack alla docu ufficiale
WAHA (2=DEVICE→`delivered`, 3=READ→`read`, come già proposto nel #39),
verificando i valori reali emessi dalla build in uso; per Telegram valutare se
esporre un delivered sintetico (il protocollo non offre una conferma di
consegna nativa per le chat private) oppure documentare la limitazione,
mantenendo il passaggio sent→read quando arriva `UpdateReadHistoryOutbox`.

**Fix (WhatsApp, branch `fix/typing-delivered-40-41`, design in `DESIGN_FIX_40_41.md`):**
- enum ack ufficiale WAHA con costanti condivise in `backends/whatsapp_events.py`
  (`WAHA_ACK_SERVER=1`, `WAHA_ACK_DEVICE=2`, `WAHA_ACK_READ=3`, `WAHA_ACK_PLAYED=4`).
- `_event_from_ack`: soglia `status >= 2` per produrre il receipt (DEVICE →
  delivered/`is_read=False`), `is_read = status >= 3` (READ). `_ack_value`
  rimappa i nomi ufficiali (alias Baileys rimossi, mai emessi da WAHA).
- `fetch_history`: soglie `ack >= WAHA_ACK_DEVICE` / `>= WAHA_ACK_READ`.
- Compatibilità verificata: l'evento sintetico outgoing in `handle_webhook` non
  ha gate su `status` e l'ordinamento messaggio-prima-del-receipt resta: con
  ack=2 il flusso produce `[message, receipt(delivered)]` senza duplicati.
- Test aggiornati ai nuovi attesi (`tests/test_whatsapp_backend.py`,
  `tests/test_whatsapp_read_receipt_fix.py`, `tests/test_edit_whatsapp.py`) +
  25 nuovi in `tests/test_whatsapp_fix_40_41.py`.

**Fix (Telegram, branch `fix/typing-delivered-40-41`):** nessun `delivered`
sintetico — **protocol limitation (by design)**: MTProto per cloud chat non
espone alcuna conferma di consegna, solo la lettura (`UpdateReadHistoryOutbox`,
già gestita da `_handle_read_receipt`). La limitazione è documentata nei
docstring di `_handle_read_receipt` e `process_receipt`; `process_receipt`
mantiene il target `"delivered"` pronto se un domani MTProto introducesse una
conferma. La metà WhatsApp è gestita separatamente (allineamento enum WAHA).

---

### #32 — Le foto Telegram dello storico non sono scaricabili né apribili (`backends/telegram.py`, righe 411-427, 460-466, 482-500) ✅ RISOLTO

Il download della foto avviene solo nel gestore live. Lo storico costruisce il
placeholder senza path e, diversamente dai documenti, senza un identificatore
utile al download lazy/on-demand.

**Scenario:** aprire una chat con foto Telegram caricate dalla cronologia.

**Fix suggerito:** scaricare le foto anche durante il fetch dello storico o conservare
file/reference id e implementare il download lazy al click.

**Fix** (branch `fix/telegram-history-photos`, design in `DESIGN_FIX_32.md`):
- Download **lazy on-demand** (pattern già usato da WhatsApp): le foto e i documenti
  dello storico (e i live con download fallito) persistono `attachment_id` come
  riferimento strutturato `tgref:<chat_id>:<msg_id>` in `_message_to_chat_event`.
- `get_attachment_path` risolve in 4 passi: path vuoto → file locale esistente
  (live/legacy) → parse `tgref:` → download lazy sul loop Telethon
  (`get_input_entity` + `get_messages(ids=<int>)` + `download_media`), con nome
  deterministico `{chat_id}-{msg_id}-{nome}`, dedup su file già presente, timeout
  30s e fallimento non bloccante (→ `None`, la UI usa i fallback esistenti).
- Niente download eager in `fetch_recent_history` (limit=20 × tutti i contatti a ogni
  backend-ready), niente migrazione schema (dedup e `ChatEvent` invariati).
- Test: 11 nuovi + 2 aggiornati in `tests/test_telegram.py`, 2 aggiornati in
  `Telegram/test_telegram_backend.py`. Suite completa: 914 passed (unico fallimento
  pre-esistente non correlato in `test_address_book.py`).
- Limiti noti: le righe legacy già persistite con `attachment_id=NULL` o bare
  `msg.id` restano non apribili (dedup impedisce la riscrittura); il click su una
  foto storica può bloccare la UI fino a 30s (stesso comportamento WhatsApp già
  accettato; follow-up: risoluzione del click in worker thread).

---

### #31 — Foto inviate renderizzate da cache allineate come ricevute e senza colore `$success` (`tui/chat_view.py`, righe 567-577) ✅ RISOLTO

Nel ramo cache `_build_message_widgets` crea l'`ImageWidget` senza assegnare
`msg-left`/`msg-right` in base a `is_mine`, a differenza del rendering live
(`_render_image_in_chat`, righe 230 e 240). Una foto **inviata**
(`is_mine=True`) caricata da cache perde così due proprietà visive: resta
allineata a sinistra come una foto ricevuta e non riceve il colore `$success`
che `msg-right` applicherebbe (`tui/css.py`, righe 98-102). L'attribuzione è
ingannevole per l'utente, che non distingue le proprie foto inviate da quelle
ricevute. Il difetto riguarda qualsiasi protocollo che passa dal ramo cache
(Signal, Telegram, WhatsApp); l'esempio più evidente è una foto WhatsApp inviata
riaperta da cache.

**Scenario:** riaprire una chat e vedere una propria foto inviata (es. WhatsApp)
allineata a sinistra e nel colore `$text` anziché `$success`, come se fosse un
messaggio ricevuto.

**Fix suggerito:** assegnare `msg-left`/`msg-right` in base a `is_mine` anche nel
ramo cache, come già fatto nel percorso live.

**Fix:** `_build_message_widgets` ora assegna `widget.classes = "msg-right" if
is_mine else "msg-left"` all'`ImageWidget` (stessa logica del live). Fix applicato
contestualmente al #36 (branch `fix/image-caption-alignment`, design in
`DESIGN_FIX_31_36.md`). Test: `TestCacheImageAlignment` in `tests/test_image_caption.py`
(parametrizzato su Signal/WhatsApp/Telegram). Suite completa 903 passed (unico
fallimento pre-esistente non correlato in `test_address_book.py`) ✅.

---

### #36 — La caption delle foto non è mai una bolla di testo dedicata: Signal/WhatsApp la mostrano solo nel placeholder da cache e il live la sovrascrive col filename, Telegram la perde del tutto (`backends/signal.py`, righe 476-489; `backends/whatsapp_events.py`, righe 162, 184-186, 205-215, 233-239; `backends/telegram.py`, righe 711, 731-733; `tui/chat_view.py`, righe 117-124, 265-276, 567-577) ✅ RISOLTO

La caption della foto (inviata o ricevuta) non è mai renderizzata come messaggio di
testo dedicato, su nessuno dei tre protocolli.

- **Signal / WhatsApp:** la caption è catturata in `attachment_info`
  (`backends/signal.py`, righe 476-489; `backends/whatsapp_events.py`, righe 184-186,
  205-215, 233-239), ma per i messaggi immagine è usata solo come etichetta del
  placeholder. Dal ramo cache appare come `[🖼️ <caption>]` (`tui/chat_view.py`,
  righe 567-577), mentre nel percorso live viene sovrascritta dal filename in
  `_finish_attachment_resolve` (`tui/chat_view.py`, righe 265-276), quindi sparisce
  appena l'allegato è risolto.
- **Telegram:** la caption è presente in `text` (`backends/telegram.py`, riga 711),
  ma `attachment_info` è hardcoded a `"🖼️ Photo"` (riga 733) e ha precedenza su
  `text`: la caption non compare mai e il placeholder da cache diventa
  `[🖼️ 🖼️ Photo]` (doppia emoji).

La modale `ImageModalScreen` (`ui_components.py`) mostra solo l'immagine.

**Scenario:** ricevere o inviare una foto con didascalia su Signal, WhatsApp o Telegram.

**Impatto:** la didascalia è indisponibile all'utente come testo dedicato su tutti i
protocolli — su Telegram in modo totale (con placeholder ridondante), su
Signal/WhatsApp solo fuori dal percorso live.

**Fix suggerito:** renderizzare `attachment_info` (o la caption Telegram in `text`)
come testo dedicato accanto/sotto il placeholder — o nella modale — distinguendo la
caption reale da mime/filename tecnici, ed evitare di hardcodare `"🖼️ Photo"`
quando è disponibile una caption in `text`.

**Fix** (branch `fix/image-caption-alignment`, design in `DESIGN_FIX_31_36.md`):
- `tui/chat_view.py`: aggiunto il resolver `_image_caption()` (euristica UI-side,
  nessun campo/schema nuovo) che distingue la caption reale dalle etichette tecniche
  (filename, mime, fallback, testi sintetici `Media:`/`<label>: <id>`).
- La caption è ora una bolla `MessageWidget` dedicata (stesso allineamento/status/
  sender della foto) montata sotto l'`ImageWidget`, sia nel percorso **live**
  (`_add_message`) sia in **cache/storico** (`_build_message_widgets`). Vale per foto
  inviate **e** ricevute su tutti e tre i protocolli.
- `backends/telegram.py`: `attachment_info = text or "Photo"` (rimosso l'hardcode
  `"🖼️ Photo"` che sopprimeva la caption Telegram, che vive in `msg.text`).
- Normalizzazione placeholder: niente doppia emoji `[🖼️ 🖼️ Photo]` e niente caption
  duplicata dentro le quadre quando la caption vive nella bolla.
- `backends/whatsapp_events.py`: WAHA consegna la caption dei media in `payload.body`
  (documentazione ufficiale); la caption ora considera anche `body`/`text`, quindi
  `attachment_info` porta la caption reale invece di cascare sul mime (`image/jpeg`).
- `backends/whatsapp.py` (percorso ack): stessa sorgente caption per i media inviati,
  e fix del **doppio rendering**: per gli outgoing `_message_already_cached` ora
  confronta l'`id` PRIMA del testo. Senza questo, l'evento sintetico `message.ack`
  (text=caption, senza `hasMedia`, stesso `msg_id`) non veniva deduplicato contro il
  messaggio media reale (`text="Media: <url>"`) e la caption compariva due volte
  (bolla caption + bolla di testo). Stessa precedenza id-su-testo nel mirror UI
  (`_merge_backend_cache._find_existing`).
- Test: `tests/test_image_caption.py` (suite `TestImageCaptionResolver`,
  `TestCaptionBubbleLive`, `TestCaptionBubbleCache`) + `test_message_photo_with_caption_uses_text_as_info`
  in `tests/test_telegram.py` + `test_hasMedia_caption_in_body`/`test_hasMedia_no_caption_keeps_mime`/
  `test_handle_webhook_image_ack_caption_in_body`/`test_ack_echo_media_does_not_duplicate`/
  `test_ack_echo_text_still_dedups_optimistic`/`test_ack_echo_media_reverse_order`
  in `tests/test_whatsapp_backend.py`. Suite completa: 903 passed (unico fallimento
  pre-esistente e non correlato: `tests/test_address_book.py::TestWAMerge::test_lid_unresolved_standalone_no_network`).
- Caso limite noto (documentato nel docstring di `_image_caption`): una caption utente
  identica a un'etichetta tecnica (es. `"photo.jpg"`) resta nel placeholder e non
  diventa bolla.
- Limite noto (bassa severità): se l'`ack` arrivasse PRIMA dell'echo media (ordine
  inverso), il dedup per id lascerebbe 1 entry di tipo `text`; in pratica WAHA emette
  l'echo `message` prima dell'`ack`, quindi lo scenario è improbabile.

---

### #35 — Read receipt Telegram non riflessa in UI dopo riconnessione/re-link (`backends/telegram.py` `_reconcile_read_state`; `tui/backend_connect.py` `_on_backend_ready`) ✅ RISOLTO

`_reconcile_read_state` marca `status="read"` in cache e SQLite **prima** di accodare
l'evento receipt. Quando il TUI processa quell'evento, `process_receipt` trova il
messaggio già `read` e restituisce `[]` → `_handle_receipt_event` esce senza fare il
mirror nella UI cache. Su un riavvio completo è innocuo (la UI cache è vuota e viene
popolata con lo stato già riconciliato), ma su una **riconnessione/re-link a caldo**
(Ctrl+L → QR flow → `_connect_telegram`) la UI cache è già popolata con `sent` e
`_on_backend_ready` salta i messaggi con id già visto senza aggiornarne lo status:
un messaggio letto a TUI chiusa resta visualizzato `sent` fino al riavvio completo.

**Scenario:** il destinatario legge un messaggio mentre la TUI è chiusa, poi l'utente
effettua un re-link senza riavviare completamente l'applicazione.

**Fix suggerito:** far accodare a `_reconcile_read_state` solo l'evento (lasciando a
`process_receipt` mutazione + mirror UI), oppure far aggiornare anche lo status in
`_on_backend_ready` per gli id già visti (non solo il dedup). L'attuale evento receipt
di riconciliazione è di fatto codice morto.

**Fix:** S2 del fix WhatsApp (PR #18) — `_on_backend_ready` e `_merge_backend_cache` ora
aggiornano lo status (rank-guard) delle entry già presenti invece di saltarle. Resta
solo l'evento receipt di `_reconcile_read_state` come codice morto (cosmetico, non funzionale).

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
| #25 | Webhook WhatsApp multi-allegato perdeva tutti gli elementi dopo il primo | Dedup webhook effimero per `(contact, parent msg_id, testo sintetico)`: gli attachment dello stesso messaggio restano distinti. `parent msg_id`, DB e schema restano invariati; nessuna migrazione. Fix validata. |
| #29 | Le reply Telegram erano visualizzate ma inviate come messaggi normali | ID Telegram originale propagato e persistito, passato a Telethon come `reply_to`; reply senza ID bloccata esplicitamente senza fallback normale e supporto retry sicuro. |
