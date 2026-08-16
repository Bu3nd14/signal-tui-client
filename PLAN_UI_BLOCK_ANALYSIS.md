# Piano di analisi dei blocchi UI (freeze) — Signal TUI Client

**Stato:** IPOTESI VALIDATE (misure + test, nessun fix). In attesa di decisione su tempi/priorità dei fix.
**Data preparazione:** 2026-08-15
**Sintomo segnalato dall'utente:** la UI resta bloccata a intermittenza durante la
*digitazione* dei messaggi e durante l'*invio* dei messaggi.

> **Nota importante:** questo documento è un *piano*, non l'analisi già svolta.
> Le "ipotesi preliminari" della sezione 5 sono punti di partenza fondati su una
> prima lettura del codice; **devono essere verificate** (staticamente, dinamicamente
> e con test) prima di essere considerate cause confermate.

> **Esito code review (2026-08-15)** — review statica completata su H1–H17.
> **Confermate: 9** (H1, H2, H3, H4, H5, H6, H9, H10, H13) · **Smentite: 1** (H11) ·
> **Da rifinire: 7** (H7, H8, H12, H14, H15, H16, H17) · **Nuove: 8** (H18–H25).
> Verdetti e riferimenti corretti in §5; matrice §6 aggiornata. Nessun fix implementato.

> **Esito validazione (2026-08-15, misure + test, nessun fix):** root cause dominante
> = **layer persistenza SQLite sincrono sul thread UI** (H2 + H1/H5 + H3). Dettagli in
> "Sintesi di validazione" qui sotto.

---

## Sintesi di validazione (esito di misure + test)

Verifica dinamica (Sviluppatore) e validazione con test (Tester) eseguite **senza
modifiche al codice applicativo e senza toccare il DB reale** (script in
`/tmp/user/1000/opencode/ui-block-analysis/`). I due agenti convergono sulla stessa
root cause.

### Cause confermate per i due sintomi

**Invio bloccato** → root cause: I/O SQLite sincrono sul thread UI + indice ricreato
a ogni write + contesa lock.
- **H1/H5** — `_add_message_to_cache` su thread UI in `on_input_submitted` (`send.py:86` → `db.py:152`): ~30 ms (DB vuoto) → 147 ms (50k) → 350 ms (120k righe).
- **H2** — `_init_db()` → `_migrate_protocol_schema` esegue `DROP INDEX`+`CREATE INDEX` **a ogni scrittura** (`db.py:61-65,98`): 17 ms (vuoto) → 334 ms (120k). **Root cause principale.**
- **H3** — contesa `_DB_LOCK` UI↔poll: attesa extra fino a ~137 ms.

**Digitazione lenta** → NON dagli handler per-keystroke (costo trascurabile ~1–6 µs),
ma da lavoro sincrono che cade sul thread UI mentre si digita:
- **H3** — contesa lock col poll worker che scrive su DB (freeze casuali).
- **H18** — `_select_contact` → `mark_read_sync` (SQLite ~55 ms + POST HTTP WAHA 30 s) subito dopo il cambio chat.
- **H7** (flush lista contatti ~10 ms ogni ~1 s) e **H21** (ricevute) come contributi additivi.
- **H9/H10/H20** — handler eseguiti a ogni tasto ma impatto trascurabile (query_one ~1 µs, hide_suggestions ~6 µs, scan emoji <2.4 ms) → **non sono la causa**.

### Bug confermati (da prioritizzare; nessun fix applicato)

| # | Causa (ipotesi) | Effetto misurato | Gravità suggerita |
|---|---|---|---|
| 1 | H2 — drop+create indice a ogni write | +17→334 ms per messaggio | Critica (root cause) |
| 2 | H1/H5 — scrittura SQLite sincrona su UI | 30→350 ms per invio | Critica (root cause) |
| 3 | H3 — contesa `_DB_LOCK` | +fino a 137 ms attesa | Critica |
| 4 | H18 — `mark_read_sync` (SQLite+HTTP) su UI | 55 ms + HTTP (30 s timeout WAHA) | Alta |
| 5 | H8 — `_load_all_messages` mount non raggruppato | 320 ms (200 msg) → 2.3 s (500) | Alta |
| 6 | H22/H24 — download allegati sincrono su UI | blocco rete (60 s timeout) | Alta (richiede WAHA live) |
| 7 | H19 — `_recompute_unread` full O(N×M) | ~52 ms per batch >4 chat | Media |
| 8 | H23 — `RichText.from_ansi` su catimg | ~333 ms (190 KB) | Media (fuori path digitazione/invio) |

### Ipotesi declassate / smentite / da misurare live

- **Smentite o non critiche:** H10 (scan emoji <2.4 ms), H11 (nessun markup), H4/H25
  (O(N) ma trascurabili alle taglie reali), H9/H20 (costo µs, pur eseguite a ogni tasto).
- **Da misurare su app live (WAHA/terminale):** H14/H18 (parte HTTP), H15, H22, H24,
  H17, magnitudine reale di H3.

### Raccomandazione per la decisione

Il fix prioritario è sul **layer di persistenza (H2 + H1/H5 + H3)**: è la causa
dominante di entrambi i sintomi e un fix mirato (gate di versione della migrazione →
non droppare l'indice a ogni write + spostare/raggruppare la scrittura fuori dal
thread UI) eliminerebbe la quota maggiore dei freeze. Il resto (H18, H8, download,
ANSI) sono bug separati con priorità inferiore.

---

## 1. Obiettivo

Individuare, con metodologia strutturata (Fishbone + FMEA), le cause per cui la UI
del client TUI (Textual) si blocca/freeza durante:

1. **Digitazione** nel campo messaggio (`#message-input`);
2. **Invio** di un messaggio (pressione di Enter / `Input.Submitted`).

Esito atteso: un **report di diagnosi** con le cause confermate, ordinate per
criticità (RPN), ciascuna corredata da evidenza (statica + dinamica + test) e da
una raccomandazione di fix (da implementare in una fase successiva, *fuori scope*).

---

## 2. Contesto architetturale (ciò che già sappiamo)

L'app è basata su **Textual** (event loop asincrono). I thread/worker in gioco:

| Contesto | Dove gira | Cosa fa |
|---|---|---|
| **Thread UI** (event loop Textual) | main | composizione, input, `on_input_submitted`, `on_input_changed`, `_add_message`, `_reorder_contact_list`, `_update_typing_label`, `_status` |
| **Thread poll** | `run_worker(..., thread=True)` — `tui/polling.py:_poll_worker` | drain eventi da tutti i backend, `_handle_event` (ingest DB + cache), poi `call_from_thread` per gli effetti UI |
| **Thread invio** | `run_worker(..., thread=True)` — `tui/send.py:_send_message_worker` | `backend.send_message_sync(...)` (rete) |
| **Thread backend** | signal SSE, WAHA, loop Telethon | connessioni, ricezione real-time |

**Persistenza SQLite** (`backend/db.py`): un singolo file `messages.db` (WAL),
protetto da `_DB_LOCK = threading.RLock()`. **Ogni** operazione di scrittura apre e
chiude una propria connessione e chiama `_init_db()` (che a sua volta esegue la
migrazione schema — vedi ipotesi H2).

**Punti chiave emersi dalla lettura preliminare (da confermare):**
- L'invio **ottimistico** (salvataggio in DB + cache + rendering del messaggio) avviene
  **sul thread UI** in `on_input_submitted` (`tui/send.py`), *prima* di delegare il
  solo invio di rete a un worker thread.
- Il **poll worker** scrive anch'esso su SQLite (via `_handle_event` → `ingest_message`),
  quindi **UI e poll contendono lo stesso `_DB_LOCK`**.
- Il rendering della lista contatti (`_reorder_contact_list`) ricostruisce la lista per
  intero ed è chiamato (via `call_from_thread`) a fine di ogni batch di poll.

---

## 3. Metodologia

Si combinano due tecniche complementari:

1. **Diagramma di Ishikawa (Fishbone)** per l'enumerazione esaustiva delle cause
   candidate, raggruppate in 6 categorie (vedi §5).
2. **FMEA** (Failure Mode and Effects Analysis) per *priorizzare*: ogni causa è un
   "modo di guasto" valutato con Severità (S), Occorrenza (O), Rilevabilità (D),
   da cui **RPN = S × O × D**. Le cause con RPN più alto guidano la verifica e poi
   gli eventuali fix.

**Scale (1–10):**

| Punteggio | Severità (impatto utente) | Occorrenza (frequenza) | Rilevabilità (facilità di intercettare prima) |
|---|---|---|---|
| 1–3 | trascurabile | rara | quasi certamente rilevata |
| 4–6 | moderato | occasionale | rilevata solo con test mirati |
| 7–9 | grave (freeze percepibile) | frequente | difficile da rilevare |
| 10 | blocco totale/riavvio | sistematica | non rilevabile |

**Soglie di "blocco percepibile" (riferimento per le misure):**
- > 16 ms di lavoro sincrono sul thread UI → superato il budget di un frame (~60 fps);
- > 50 ms → micro-freeze percettibile;
- > 200 ms → freeze chiaramente visibile;
- > 500 ms → blocco grave.

**Criterio di chiusura del task:** ogni ipotesi deve risultare *confermata* (con
evidenza) oppure *esclusa* (con motivazione), e le cause confermate devono avere
una misura del loro contributo al freeze (es. "H2 aggiunge ~X ms per messaggio").

---

## 4. Ruoli e responsabilità

| Ruolo | Responsabilità principali | Deliverable |
|---|---|---|
| **Architetto** | Guida Fishbone/FMEA, definisce/valida le scale e gli RPN, decide l'ordine di verifica, propone il design dei fix (solo design, non implementazione) | Diagramma Fishbone compilato + matrice FMEA con S/O/D/RPN + raccomandazioni |
| **Sviluppatore** | Verifica statica (trace dei path sincroni) e dinamica (strumentazione/profilazione); produce evidenze misurabili per ogni ipotesi | Report di verifica codice con misure (tempi, stack, lock) |
| **Tester** | Scrive/esegue test che riproducono e misurano i blocchi; valida/smentisce le ipotesi; segnala bug | Suite di test + report esiti (pass/fail con soglie) |

**Flusso iterativo:**
```
Architetto (ipotesi + priorità)
   → Sviluppatore (verifica statica + dinamica)
      → Tester (test di conferma)
         → Architetto (chiude FMEA, consolida il report finale)
```

**Interdipendenze:**
- Il Tester ha bisogno dell'elenco ipotesi priorizzato (da Architetto) e dei punti
  d'innesco/strumentazione (da Sviluppatore) per scrivere test mirati.
- Se il Tester smentisce un'ipotesi o il Sviluppatore non trova il path nel codice,
  l'ipotesi torna all'Architetto per declassamento/esclusione.

---

## 5. Diagramma di Ishikawa — categorie e ipotesi (con esito della review statica)

> Stato di verifica (review statica 2026-08-15): **CONFERMATA** = path/struttura
> confermati nel codice con riga precisa; **SMENTITA** = esclusa; **DA RIFINIRE** =
> correzione di riga/thread/costo necessaria prima della misura dinamica.

### Categoria A — Persistenza / SQLite (la più sospetta per l'**invio**)

- **H1 — Scrittura SQLite sincrona sul thread UI in fase di invio.** → **CONFERMATA**
  `tui/send.py:33 on_input_submitted` → `send.py:86 ingest_message` (sul thread UI)
  → `backends/signal.py:653 ingest_message` → `_message_already_cached`
  (`signal.py:641`) + `_add_message_to_cache` (`backend/db.py:152`), che esegue
  `_init_db()` + `sqlite3.connect` + INSERT + `commit` + `close` (db.py:174-202), tutto
  **sincrono sul thread UI**. Confermato: l'invio ottimistico scrive su SQLite prima di
  delegare la sola rete al worker (send.py:124).

- **H2 — L'indice `idx_messages_contact` viene droppato e ricreato a ogni scrittura.** → **CONFERMATA**
  `_init_db` (`db.py:68`) chiama `_migrate_protocol_schema` (`db.py:98`), che esegue
  **incondizionatamente** `DROP INDEX IF EXISTS` + `CREATE INDEX IF NOT EXISTS`
  (`db.py:61-65`), senza gate di versione. `_init_db()` è invocato da OGNI helper di
  scrittura: `_add_message_to_cache:174`, `_mark_as_read:266`, `_update_message_status:313`,
  `_update_message_id:221`, `_prune_cache:239`, `_dedup_messages:286`, `_count_unread:329`,
  `_load_cache:112`. Ogni singolo messaggio scatena quindi un drop+create indice.

- **H3 — Contesa su `_DB_LOCK` tra thread UI e thread poll.** → **CONFERMATA (strutturale)**
  `_DB_LOCK` RLock condiviso (`db.py:24`). Il thread UI lo acquisisce su invio
  (send.py:86 → db.py:175) e su selezione contatto (contacts.py:406 → `_mark_as_read`
  db.py:267); il poll worker lo acquisisce su ingest (events.py:87 → db.py:175);
  `_init_db` lo ri-acquisisce (db.py:76, RLock re-entrante). La contesa è reale; **la
  magnitudine (attesa tipica/massima) resta da misurare** (§8.4).

- **H4 — `_message_already_cached` è O(N) sulla cache in-memory del contatto.** → **CONFERMATA**
  `backends/signal.py:641` (loop su `self.cache.get(contact_id, [])`); idem
  `whatsapp.py:812` e `telegram.py:661`. Scansione lineare a ogni ingest: sul thread UI
  in invio (send.py:86) e sul poll worker. N è limitato dalla retention (200/contatto in
  DB, 50 Telegram) ma cresce in sessione.

- **H5 — Apertura/chiusura connessione per ogni scrittura.** → **CONFERMATA**
  Ogni helper DB fa `sqlite3.connect` + `close` senza riuso (db.py:176-202 e analoghi).
  Costo fisso per chiamata, secondario rispetto a H2 ma reale nei burst.

### Categoria B — Rendering / Layout Textual (invio e arrivo messaggi)

- **H6 — Mount + layout pass per ogni messaggio.** → **CONFERMATA**
  `tui/chat_view.py:146-147` `chat_log.mount(widget)` + `scroll_end(animate=False)` per
  messaggio (sul thread UI: diretto su invio, via `call_from_thread` su arrivo). Ogni
  mount invalida il layout; N messaggi = N pass.

- **H7 — Rebuild completo della lista contatti a fine batch di poll.** → **DA RIFINIRE**
  `tui/polling.py:71-88` → `call_from_thread(self._reorder_contact_list)` →
  `tui/contacts.py:46`. **Correzione:** non è più un "rebuild completo": `_render_contact_list`
  (contacts.py:206-301) ha un fast-path in-place a ordine invariato (250-253) e un reorder
  in-place via `move_child` (254-269); il clear+rebuild avviene solo se l'insieme cambia
  (294-297, progressivo). **Restano** comunque O(N) sul thread UI per flush (~1s):
  `_sort_contacts` O(N log N) + `_apply_contact_visibility` + `_sync_contact_highlight`
  O(N) (contacts.py:301, 141-153). Misurare con N=350-600.

- **H8 — `_load_all_messages` / `_render_chat_window` montano molti widget.** → **DA RIFINIRE**
  `_render_chat_window` monta la finestra da 20 con UN solo `chat_log.mount(*widgets)`
  (chat_view.py:412-414) — già ottimizzato; il `sorted()` della cache avviene nel
  worker (chat_view.py:343), non sul thread UI. **`_load_all_messages` invece monta un
  widget per messaggio** via `_add_message` (chat_view.py:588, mount+scroll_end per
  messaggio) sul thread UI: unbounded sul click "load more". Solo quest'ultimo è un
  reale rischio freeze.

### Categoria C — Handler per-keystroke (la più sospetta per la **digitazione**)

- **H9 — `on_input_changed` esegue un `query_one` (scansione DOM) a ogni tasto.** → **CONFERMATA**
  `tui/pickers.py:106`; `query_one("#emoji-completion")` nel ramo `:` (pickers.py:123) e
  nel ramo else (pickers.py:131, a OGNI keystroke senza `:`). Confermato.

- **H10 — `get_emoji_suggestions` itera l'intero dizionario emoji a ogni keystroke.** → **CONFERMATA (con nuance)**
  `emoji_picker.py:74-84`: itera `_EMOJI_TO_ALIAS` (~3500 voci) con
  `.replace("_"," ").replace("-"," ")` per voce, ma **esce a 10 risultati** (riga 82):
  costo sub-lineare per prefissi comuni, O(N) pieno per prefissi rari/senza match.
  `show_suggestions` (622) → `_rebuild()` (588) ricrea fino a 10 widget solo quando la
  lista cambia (630).

- **H11 — Markup/rich render dell'`Input` a ogni keystroke.** → **SMENTITA**
  `#message-input` è creato senza `markup=True` (`ui_components.py:92`). L'`Input` di
  Textual (8.2.8) renderizza `Text(self.value, ...)` senza parsing markup
  (`textual/widgets/_input.py:687`). Nessun re-parse Rich per keystroke.

### Categoria D — Event storm dal poll worker

- **H12 — Raffiche typing/ricevute che saturano il thread UI.** → **DA RIFINIRE**
  Il fix in-place esiste (`events.py:271 _update_typing_label`). **Correzione di thread:**
  l'iterazione dei dict typing in `polling.py:35-65` avviene nel **worker thread** (non
  UI); gli effetti UI sono i `call_from_thread` (polling.py:48,63). Sul thread UI resta
  `_update_message_widgets_status` (events.py:204): mappa ts→widget O(M) sui figli del
  chat log + fallback fuzzy O(M) per ogni miss (vedi H21).

- **H13 — `call_from_thread` frequenti durante burst.** → **CONFERMATA**
  Ogni messaggio del contatto corrente programma `call_from_thread(self._add_message,…)`
  (events.py:136) + ogni typing `call_from_thread(_update_typing_label)` (events.py:268).
  N eventi = N callback UI ravvicinate, ciascuna con mount+scroll_end.

### Categoria E — Backend / Rete

- **H14 — Percorsi di invio sincroni rimasti sul thread UI.** → **DA RIFINIRE**
  L'invio di rete è SOLO nel worker (`_send_message_worker`, send.py:171). **Ma** esiste un
  percorso sincrono rete+DB sul thread UI: `_select_contact` → `mark_read_sync`
  (contacts.py:406-408); per WhatsApp `mark_read_sync` fa POST HTTP bloccante
  (`whatsapp.py:637` → `whatsapp_rest.mark_read`, urlopen timeout 30s) + `_mark_as_read`
  SQLite. Vedi H18. Inoltre `on_exit` → `disconnect_sync` blocca il thread UI fino a
  ~10s ma solo all'uscita (telegram.py:103-115).

- **H15 — `fetch_history` WhatsApp e loop Telethon.** → **DA RIFINIRE**
  La rete gira in worker (`_load_messages_worker` thread, chat_view.py:296; loop Telethon
  dedicato). Il render finale avviene sul thread UI ma è **un solo mount batch di ≤20
  widget** (`_mount_window` via `call_from_thread`, chat_view.py:412-425): impatto
  limitato, non un full-render.

### Categoria F — Ambiente / Risorse

- **H16 — Dimensione DB / frammentazione / checkpoint WAL.** → **DA RIFINIRE**
  Non verificabile staticamente; amplifica H1/H2/H5 (WAL attivo, db.py:79). Serve misura
  dinamica su DB popolato (crescita `messages.db`, tempi checkpoint).

- **H17 — Terminale lento o rendering parziale.** → **DA RIFINIRE**
  Non verificabile staticamente; serve riproduzione cross-terminale/SSH.

### Nuove ipotesi emerse dalla review statica (H18+)

- **H18 (Cat. A) — `_select_contact` esegue lavoro bloccante sul thread UI, incluso `mark_read_sync` (SQLite + eventuale HTTP).**
  `contacts.py:355-441`: `_clear_chat` + 3× `_add_message` (381-388) + `mark_read_sync`
  (406-408) → `_mark_as_read` (db.py:264, con `_init_db`+drop/create indice) e, per
  WhatsApp, POST HTTP bloccante `rest.mark_read` (whatsapp.py:637, timeout 30s) +
  `_sync_contact_highlight` O(N) (435). Freeze percepibile **subito dopo la selezione di
  un contatto**, esattamente quando si inizia a digitare.

- **H19 (Cat. A) — `_recompute_unread` "full" O(N×M) sul thread UI.**
  `polling.py:82-84` (batch > `_CONTACT_UPDATE_BATCH_MAX`) → `call_from_thread(self._recompute_unread)`
  → `unread_reply.py:51-59` itera tutti i contatti × messaggi (in-memory, ma sincrono sul
  thread UI). Stesso costo in `_update_unread_badges()` a backend-ready (backend_connect.py:101).

- **H20 (Cat. C) — `hide_suggestions()` esegue `remove_children()` + `remove_class` incondizionati a ogni keystroke senza `:`.** 
  `pickers.py:130-134` → `emoji_picker.py:640-645`: anche quando il widget è già
  vuoto/nascosto, ogni tasto senza `:` fa `remove_children` + `remove_class` sul DOM.
  (Distinto da H9, che è il `query_one`.)

- **H21 (Cat. D) — `_update_message_widgets_status` O(M) sul thread UI per batch di ricevute.**
  `events.py:204-233`: costruisce `by_ts` iterando tutti i figli del chat log O(M) +
  fallback fuzzy O(M) per ogni ricevuta senza match esatto (230-233). Con chat lunghe e
  raffiche di ricevute satura il thread UI.

- **H22 (Cat. E) — Download (modalità download): rete + I/O file sincroni sul thread UI.**
  `download.py:47-89 _start_download` (invocato da `on_message_widget_message_clicked`,
  thread UI): per allegati WhatsApp `get_attachment_path` → `download_media`
  (whatsapp.py:686 → whatsapp_rest.py:335, urlopen timeout 60s) + `write_bytes` +
  `_serve_file_path`/`serve_text_as_file` (I/O filesystem). Non sul path digitazione/invio,
  ma freeze grave in modalità download.

- **H23 (Cat. B) — `ImageModalScreen` parsa l'output ANSI di `catimg` sul thread UI.**
  `ui_components.py:401-441`: il subprocess è async (non blocca), ma
  `RichText.from_ansi(ansi_output)` + `img.write(...)` (441) processano l'ANSI sul thread
  UI; per immagini grandi il parsing è costoso. (Costo da misurare.)

- **H24 (Cat. E) — Rendering di immagini WhatsApp scarica l'allegato in modo sincrono sul thread UI.**
  `_add_message` → `_render_image_in_chat` (chat_view.py:98-105 → 185-228) →
  `manager.get_attachment_path` → `WhatsAppBackend.get_attachment_path`
  (whatsapp.py:661-695): se il file non è in cache fa `download_media` (rete, timeout 60s)
  + `write_bytes` sul thread UI. Freeze all'arrivo/visualizzazione di un'immagine
  WhatsApp, anche mentre si digita.

- **H25 (Cat. C/B) — `_refresh_chat` calcola `max()` su `_seen_timestamps` O(N) + `sorted(cache)` sul thread UI.**
  `chat_view.py:603-626`: `max((t for (_p,_k,t) in self._seen_timestamps), default=0)`
  (626) + `sorted(cached, ...)` (623). Chiamato alla chiusura dell'emoji picker
  (pickers.py:40) e del contact picker (pickers.py:65). `_seen_timestamps` cresce con la
  chat (svuotato solo in `_select_contact`/`_load_all_messages`): O(N), freeze potenziale
  all'uscita dai picker.

> **Esclusa dalla review (non sul path digitazione/invio):** `_status()` + `set_timer`
> (app.py:230-252) non è chiamato dal path digitazione/invio (solo errori/connessione/
> apertura chat), quindi nessun churn per keystroke. Nessun `watch_*`/`validate_*`/computed
> CSS custom sul `#message-input` (grep: nessun match); l'unico watcher reattivo è quello
> built-in di Textual `Input._watch_value` → `Changed` (già coperto da H9/H20).

> **Priorità iniziale suggerita** (da rivalutare con S/O/D): H1, H2, H3, H9, H10 restano i
> candidati principali; aggiungere **H18** e **H24** come critici (blocco certo e misurabile
> sul thread UI).

## 6. Matrice FMEA (template da compilare)

L'Architetto consolida qui l'esito. Ogni riga = un modo di guasto.
Colonna **Stato** = esito della review statica (C = confermata, S = smentita,
R = da rifinire). S/O/D/RPN restano da compilare in sessione (soglie §3).

| ID | Stato | Categoria | Modo di guasto | Effetto | Evidenza (rif. codice) | S | O | D | RPN | Verifica (statica/dinamica/test) | Decisione |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H1 | C | A | Scrittura SQLite sincrona su thread UI | freeze all'invio | `send.py:86` + `db.py:152,174-202` | | | | | profilo `on_input_submitted` | |
| H2 | C | A | Drop+create indice a ogni write | freeze invio/ricezione | `db.py:61-65,98,68,174` | | | | | assert idempotenza migrazione (T1) | |
| H3 | C | A | Contesa `_DB_LOCK` UI vs poll | micro-freeze casuali | `db.py:24,76,175,267` | | | | | test concorrenza lock (T7) | |
| H4 | C | A | Dedup `_message_already_cached` O(N) | latenza ingest | `signal.py:641`; `whatsapp.py:812`; `telegram.py:661` | | | | | T3 | |
| H5 | C | A | connect/close per write | costo fisso per write | `db.py:176-202` | | | | | T2 | |
| H6 | C | B | Mount+scroll per messaggio | freeze su burst | `chat_view.py:146-147` | | | | | T8/T9 | |
| H7 | R | B | Re-sort+render O(N) lista contatti a ogni batch | micro-freeze ~1s | `polling.py:88`; `contacts.py:46,206-301` | | | | | T6 | |
| H8 | R | B | `_load_all_messages` mount unbounded | freeze su "load more" | `chat_view.py:553-601,588` | | | | | T8 | |
| H9 | C | C | `query_one` a ogni keystroke | micro-lag digitazione | `pickers.py:106,123,131` | | | | | T5 | |
| H10 | C | C | Scan `_EMOJI_TO_ALIAS` per prefisso | lag digitazione `:` | `emoji_picker.py:74-84,622,588` | | | | | T4 | |
| H11 | S | C | Markup re-render Input | (escluso) | `ui_components.py:92`; nessun `markup=True` | | | | | — | |
| H12 | R | D | Storm typing/ricevute | saturazione UI | `events.py:271`; `polling.py:35-65` (worker) | | | | | T9 | |
| H13 | C | D | `call_from_thread` per evento | N callback UI | `events.py:136,268` | | | | | T9 | |
| H14 | R | E | Invio sincrono su UI | (rete solo in worker) | `send.py:171`; ma `contacts.py:406` (vedi H18) | | | | | audit grep | |
| H15 | R | E | fetch_history → render UI | micro-lag apertura chat | `chat_view.py:296,412-425` | | | | | T8 | |
| H16 | R | F | DB grande/WAL checkpoint | amplifica H1-H5 | `db.py:79` | | | | | misura DB popolato | |
| H17 | R | F | Terminale/SSH lento | freeze ambiente | — | | | | | test cross-terminale | |
| H18 | C | A | `mark_read_sync` (SQLite+HTTP WAHA) su UI in `_select_contact` | freeze su cambio chat | `contacts.py:406-408`; `whatsapp.py:637`; `db.py:264` | | | | | profilo `_select_contact` | |
| H19 | C | A | `_recompute_unread` full O(N×M) su UI | freeze a fine batch grande | `polling.py:84`; `unread_reply.py:51-59` | | | | | T6 con batch grande | |
| H20 | C | C | `hide_suggestions` incondizionato a ogni tasto | churn DOM per keystroke | `pickers.py:130-134`; `emoji_picker.py:640-645` | | | | | T5 | |
| H21 | C | D | `_update_message_widgets_status` O(M) su UI | freeze su raffica ricevute | `events.py:204-233` | | | | | T9 | |
| H22 | C | E | Download rete+I/O su UI (download mode) | freeze in modalità download | `download.py:47-89`; `whatsapp_rest.py:335` | | | | | test click download | |
| H23 | R | B | `RichText.from_ansi` su output catimg | freeze su apertura immagine | `ui_components.py:441` | | | | | test immagine grande | |
| H24 | C | E | Download immagine WhatsApp sincrono su UI in `_add_message` | freeze su arrivo immagine WA | `chat_view.py:98-105,205-210`; `whatsapp.py:661-695` | | | | | test arrivo immagine WA | |
| H25 | C | C/B | `max(_seen_timestamps)` O(N) + `sorted(cache)` in `_refresh_chat` | freeze all'uscita picker | `chat_view.py:623,626`; `pickers.py:40,65` | | | | | profilo `_refresh_chat` | |

(Le colonne S/O/D/RPN sono da compilare in sessione; le soglie sono in §3.)

## 7. Piano di verifica statica — Sviluppatore

Obiettivo: tracciare **tutti i path sincroni** eseguiti sul thread UI per
digitazione e invio, e identificare le chiamate bloccanti.

1. **Trace del path di invio** da `Input.Submitted` (`tui/send.py:33`) fino a dove
   si ferma: elencare ogni chiamata con (file, funzione, costo atteso). Marcare come
   *bloccante* le chiamate a I/O (SQLite, rete, subprocess) e *O(N)* sui dati.
2. **Trace del path di digitazione** da `Input.Changed` (`tui/pickers.py:106`):
   elencare handler, `query_one`, `show_suggestions`, e la complessità di
   `get_emoji_suggestions`/`_rebuild`.
3. **Grep sistematico** sul progetto per pattern bloccanti usati nel thread UI:
   `sqlite3.connect`, `.commit(`, `.execute(`, `query_one(`, `time.sleep(`, `urlopen(`,
   `.join(`, `subprocess`, `list(...children)`, `sorted(`. Per ciascun match:
   capire se eseguito sul thread UI o su worker.
4. **Verifica di H2**: leggere `_init_db`/`_migrate_protocol_schema` e confermare che
   il drop/create indice avviene a ogni chiamata; stimare il costo con un DB popolato.
5. **Mappa lock**: elencare tutti i punti che acquisiscono `_DB_LOCK` e quali thread li
   raggiungono.
6. **Conferma/affinamento righe di codice** citate in §5 (i numeri potrebbero essersi
   mossi).

**Output:** tabella "path → chiamata bloccante → thread → costo atteso", con evidenza
per confermare/smentire H1–H17.

---

## 8. Piano di verifica dinamica / profilazione — Sviluppatore

Obiettivo: misurare i tempi reali e attribuire il freeze a una causa.

1. **Strumentazione temporanea** (con `time.perf_counter`, da rimuovere a fine analisi)
   attorno a: `on_input_submitted`, `ingest_message`, `_add_message_to_cache`,
   `_init_db`/`_migrate_protocol_schema`, `on_input_changed`,
   `get_emoji_suggestions`, `_reorder_contact_list`, `_add_message`.
   Log con soglia (es. log solo se > 20 ms).
2. **Profilazione**: usare la directory `profiling/` già esistente e/o `py-spy`
   (`py-spy dump`/`record`) sul processo in esecuzione mentre si digita e si invia,
   per ottenere lo stack dove il thread UI passa più tempo.
3. **Riproduzione controllata**: scenari che riproducono i due sintomi:
   - digitazione veloce (anche con `:` per attivare il completion emoji);
   - invio ripetuto di messaggi;
   - chat con molti messaggi e lista contatti con 350–600 voci;
   - DB popolato (crescita di `messages.db`).
4. **Misura della contesa lock (H3)**: contare/timare le attese su `_DB_LOCK` dal thread UI.
5. **Misura del budget frame**: per ogni operazione del thread UI, registrare il tempo
   e confrontarlo con le soglie §3.

**Output:** misure (min/med/max/p99) per ogni punto strumentato + stack di profilo
del momento di freeze.

---

## 9. Piano di test — Tester

Obiettivo: trasformare ogni ipotesi in un test riproducibile con criterio pass/fail.

**Test funzionali/regressivi (già in stile repo, vedi `tests/`):**
- T1 (H2): `_init_db()` chiamato due volte NON deve ricreare l'indice (assert su
  `PRAGMA index_list(messages)` o sull'idempotenza); oggi si sospetta che lo faccia.
- T2 (H1/H5): misurare `_add_message_to_cache` con DB popolato (es. 50k righe) e
  asserire che il tempo resta sotto soglia (es. < 10 ms mediana); fallisce se la
  scrittura sincrona/ri-connessione è la causa.
- T3 (H4): `ingest_message` su chat da N messaggi — complessità attesa; asserire che
  la dedup non scala linearmente in modo patologico (o documentarlo).
- T4 (H10): `get_emoji_suggestions` con prefissi tipici (1–3 char) — asserire tempo
  < X ms e/o che non itera l'intero dizionario.
- T5 (H9): mockare `Input.Changed` e asserire che `on_input_changed` non fa `query_one`/
  rebuild inutili quando non c'è `:` nel valore.
- T6 (H7): `_reorder_contact_list` con N=600 contatti — misurare il tempo di rebuild.

**Test di concorrenza/contesa:**
- T7 (H3): N scritture concorrenti (UI + poll simulati) → misurare il tempo max di attesa
  del lock sul thread UI; asserire sotto soglia.

**Test end-to-end (pytest-asyncio + Textual `Pilot`):**
- T8: aprire l'app, digitare N caratteri e inviare un messaggio; misurare il tempo di
  risposta del loop (es. intervallo tra due `Input.Changed` o tra submit e mount) e
  asserire che resti sotto soglia §3.
- T9: simulare uno **storm di eventi** (typing + messaggi) e verificare che il thread UI
  non si satura (nessun blocco > soglia).

**Output:** suite test (file nella cartella `tests/`) + report esiti con tempo misurato
e verdetto per ogni ipotesi (confermata/esclusa), con rinvio al Sviluppatore in caso di
bug confermati (loop: fix → ri-test).

---

## 10. Sequenza operativa e dipendenze

1. **Architetto** — consolida Fishbone + FMEA, assegna S/O/D preliminari, fissa
   l'ordine di verifica. *(input per 2 e 3)*
2. **Sviluppatore** — verifica statica (§7) in parallelo con preparazione
   strumentazione (§8).
3. **Tester** — scrive i test (§9) basandosi sulle ipotesi prioritarie e sui punti
   d'innesco del Sviluppatore.
4. **Sviluppatore** — esegue profilazione dinamica, conferma/smentisce con misure.
5. **Tester** — esegue i test, produce report.
6. **Architetto** — chiude la FMEA (RPN finali), consolida il **report di diagnosi**
   con le cause confermate ordinate per RPN e le raccomandazioni di fix (design).
7. *(se emergono bug)* — loop Sviluppatore↔Tester finché i test passano.

---

## 11. Deliverable e criteri di accettazione

**Deliverable finali:**
- `REPORT_UI_BLOCK_ANALYSIS.md` (diagnosi): Fishbone compilato, matrice FMEA con RPN,
  cause confermate/escluse, misure, raccomandazioni di fix (design, non codice).
- Suite di test aggiunta in `tests/` (verde).
- Evidenze di profilazione (in `profiling/` o allegate al report).

**Criteri di accettazione:**
- Ogni ipotesi H1–H17 è marcata *confermata* o *esclusa* con evidenza.
- Per le cause confermate c'è una misura del contributo al freeze.
- I due sintomi (digitazione + invio) hanno almeno una causa-madre identificata.
- Nessun fix implementato in questa fase (solo raccomandazioni), salvo decisione
  esplicita dell'utente.

---

## 12. Fuori scope

- Implementazione dei fix (fase successiva).
- Analisi di performance diverse dai freeze UI (es. memoria, CPU backend).
- Modifiche funzionali ai protocolli.
